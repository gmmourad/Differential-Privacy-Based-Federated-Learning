#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.6

import torch
from torch import nn, autograd
from utils.dp_mechanism import cal_sensitivity, Laplace, Gaussian_Simple, Gaussian_moment
from torch.utils.data import DataLoader, Dataset
import numpy as np
import random
from sklearn import metrics


class DatasetSplit(Dataset):
    def __init__(self, dataset, idxs):
        self.dataset = dataset
        self.idxs = list(idxs)

    def __len__(self):
        return len(self.idxs)

    def __getitem__(self, item):
        image, label = self.dataset[self.idxs[item]]
        return image, label


class LocalUpdate(object):
    def __init__(self, args, dataset=None, idxs=None, dp_mechanism='no_dp', dp_epsilon=20, dp_delta=1e-5, dp_clip=20):
        self.args = args
        self.loss_func = nn.CrossEntropyLoss()
        self.selected_clients = []
        self.ldr_train = DataLoader(DatasetSplit(dataset, idxs), batch_size=self.args.local_bs, shuffle=True)
        self.dp_mechanism = dp_mechanism
        self.dp_epsilon = dp_epsilon
        self.dp_delta = dp_delta
        self.dp_clip = dp_clip
        self.idxs = idxs

    def train(self, net):
        net.train()
        # train and update
        optimizer = torch.optim.SGD(net.parameters(), lr=self.args.lr, momentum=self.args.momentum)
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=self.args.lr_decay)

        epoch_loss = []
        for iter in range(self.args.local_ep):
            batch_loss = []
            for batch_idx, (images, labels) in enumerate(self.ldr_train):
                images, labels = images.to(self.args.device), labels.to(self.args.device)
                net.zero_grad()
                log_probs = net(images)
                loss = self.loss_func(log_probs, labels)
                loss.backward()
                if self.dp_mechanism != 'no_dp':
                    self.clip_gradients(net, len(images))
                optimizer.step()
                scheduler.step()
                # add noises to parameters
                if self.dp_mechanism != 'no_dp':
                    self.add_noise(net)
                batch_loss.append(loss.item())
            epoch_loss.append(sum(batch_loss)/len(batch_loss))

        return net.state_dict(), sum(epoch_loss) / len(epoch_loss), scheduler.get_last_lr()[0]

    def clip_gradients(self, net, batch_size):
        if self.dp_mechanism == 'Laplace':
            # Laplace use 1 norm
            self.perSampleClip(net, batch_size, self.args.device, self.dp_clip, norm=1)
        elif self.dp_mechanism == 'Gaussian':
            # Gaussian use 2 norm
            self.perSampleClip(net, batch_size, self.args.device, self.dp_clip, norm=2)

    def perSampleClip(self, net, batch_size, device, clipping, norm):
        # per sample gradient clip by hand (using opacus)
        grads = [param.grad_sample.detach().clone() for param in net.parameters()]
        for idx in range(batch_size):
            norm_sum = torch.tensor(0.0).to(device)
            for i in range(len(grads)):
                norm_sum += (torch.norm(grads[i][idx].to(torch.float32), p=norm) ** norm)
            norm_sum = torch.pow(norm_sum, exponent=1 / norm)
            for i in range(len(grads)):
                grads[i][idx] = torch.div(grads[i][idx], torch.max(torch.tensor(1),
                                                          torch.div(norm_sum, torch.tensor(clipping)))).to(device)
        # average per sample gradient after clipping
        for i in range(len(grads)):
            grads[i] = torch.mean(grads[i], dim=0)
        # set back gradient
        for i, param in enumerate(net.parameters()):
            param.grad = grads[i]

    def add_noise(self, net):
        sensitivity = cal_sensitivity(self.args.lr, self.dp_clip, len(self.idxs))
        if self.dp_mechanism == 'Laplace':
            with torch.no_grad():
                for k, v in net.named_parameters():
                    noise = Laplace(epsilon=self.dp_epsilon, sensitivity=sensitivity, size=v.shape)
                    noise = torch.from_numpy(noise).to(self.args.device)
                    v += noise
        elif self.dp_mechanism == 'Gaussian':
            with torch.no_grad():
                for k, v in net.named_parameters():
                    noise = Gaussian_Simple(epsilon=self.dp_epsilon, delta=self.dp_delta, sensitivity=sensitivity, size=v.shape)
                    noise = torch.from_numpy(noise).to(self.args.device)
                    v += noise
