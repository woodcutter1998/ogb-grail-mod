import statistics
import timeit
import os
import logging
import pdb
import numpy as np
import time
from tqdm import tqdm

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader

from sklearn import metrics


class Trainer():
    def __init__(self, params, graph_classifier, train, state_dict=None, valid_evaluator=None):
        self.graph_classifier = graph_classifier
        self.valid_evaluator = valid_evaluator
        self.params = params
        self.train_data = train

        model_params = list(self.graph_classifier.parameters())
        logging.info('Total number of parameters: %d' % sum(map(lambda x: x.numel(), model_params)))

        if params.optimizer == "SGD":
            self.optimizer = optim.SGD(model_params, lr=params.lr, momentum=params.momentum, weight_decay=self.params.l2)
        if params.optimizer == "Adam":
            self.optimizer = optim.Adam(model_params, lr=params.lr, weight_decay=self.params.l2)

        self.updates_counter = 0
        self.start_epoch = 0
        self.epoch = 1
        if state_dict is not None:
            self.start_epoch = state_dict['epoch']
            self.optimizer.load_state_dict(state_dict['optimizer'])
            self.graph_classifier.load_state_dict(state_dict['state_dict'])

        self.criterion = nn.MarginRankingLoss(self.params.margin, reduction='sum')

        self.reset_training_state()

    def reset_training_state(self):
        self.best_metric = 0
        self.last_metric = 0
        self.not_improved_count = 0
        self.should_train = True

    def train_epoch(self):
        total_loss = 0
        all_preds = []
        all_labels = []
        all_scores = []

        dataloader = DataLoader(self.train_data, batch_size=self.params.batch_size, shuffle=True, num_workers=self.params.num_workers, collate_fn=self.params.collate_fn)
        self.graph_classifier.train()
        # model_params = list(self.graph_classifier.parameters())
        pbar = tqdm(dataloader)

        for batch in pbar:
            data_pos, targets_pos, data_neg, targets_neg = self.params.move_batch_to_device(batch, self.params.device)
            self.optimizer.zero_grad()
            score_pos = self.graph_classifier(data_pos)
            score_neg = self.graph_classifier(data_neg)
            loss = self.criterion(score_pos, score_neg.view(len(score_pos), -1).mean(dim=1), torch.Tensor([1]).to(device=self.params.device))
            # print(score_pos, score_neg, loss)
            loss.backward()
            self.optimizer.step()

            with torch.no_grad():
                all_scores += score_pos.squeeze().cpu().tolist() + score_neg.squeeze().cpu().tolist()
                all_labels += targets_pos.cpu().tolist() + targets_neg.cpu().tolist()
                total_loss += loss.item()
                
        self.updates_counter += 1

        if self.valid_evaluator and self.params.eval_every_iter and self.updates_counter % self.params.eval_every_iter == 0:
            tic = time.time()
            result = self.valid_evaluator.eval()
            print('\nPerformance:' + str(result) + 'in ' + str(time.time() - tic))

            if result['mrr'] >= self.best_metric:
                self.save_classifier()
                self.best_metric = result['mrr']
                self.not_improved_count = 0

            else:
                self.not_improved_count += 1
                if self.not_improved_count > self.params.early_stop:
                    logging.info(f"Validation performance didn\'t improve for {self.params.early_stop} epochs. Training stops.")
                    self.should_train = False
            self.last_metric = result['mrr']

        auc = metrics.roc_auc_score(all_labels, all_scores)
        auc_pr = metrics.average_precision_score(all_labels, all_scores)
        torch.cuda.empty_cache()
        # weight_norm = sum(map(lambda x: torch.norm(x), model_params))
        return total_loss/self.params.train_edges, auc, auc_pr

    def train(self):
        self.reset_training_state()

        for epoch in range(self.start_epoch+1, self.params.num_epochs + self.start_epoch + 1):
            self.epoch = epoch
            time_start = time.time()
            loss, auc, auc_pr = self.train_epoch()
            time_elapsed = time.time() - time_start
            print(f'Epoch {epoch} with loss: {loss}, training auc: {auc}, training auc_pr: {auc_pr}, best VAL mrr: {self.best_metric} in {time_elapsed}')
            if not self.should_train:
                break
            # if self.valid_evaluator and epoch % self.params.eval_every == 0:
            #     result = self.valid_evaluator.eval()
            #     logging.info('\nPerformance:' + str(result))
            
            #     if result['auc'] >= self.best_metric:
            #         self.save_classifier()
            #         self.best_metric = result['auc']
            #         self.not_improved_count = 0

            #     else:
            #         self.not_improved_count += 1
            #         if self.not_improved_count > self.params.early_stop:
            #             logging.info(f"Validation performance didn\'t improve for {self.params.early_stop} epochs. Training stops.")
            #             break
            #     self.last_metric = result['auc']

            if epoch % self.params.save_every == 0:
                torch.save({'epoch': self.epoch, 'state_dict': self.graph_classifier.state_dict(), 'optimizer': self.optimizer.state_dict()}, os.path.join(self.params.exp_dir, 'graph_classifier_chk.pth'))

    def save_classifier(self):
        torch.save({'epoch': self.epoch, 'state_dict': self.graph_classifier.state_dict(), 'optimizer': self.optimizer.state_dict()}, os.path.join(self.params.exp_dir, 'best_graph_classifier.pth'))  # Does it overwrite or fuck with the existing file?
        logging.info('Better models found w.r.t accuracy. Saved it!')
