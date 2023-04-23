import numpy as np

import torch
import torch.nn.functional as F
from torch.nn import init
from torch import nn, autograd


class HM(autograd.Function):

    @staticmethod
    def forward(ctx, inputs, indexes, features, momentum):
        ctx.features = features
        ctx.momentum = momentum
        ctx.save_for_backward(inputs, indexes)
        outputs = inputs.mm(ctx.features.t())

        return outputs

    @staticmethod
    def backward(ctx, grad_outputs):
        inputs, indexes = ctx.saved_tensors
        grad_inputs = None
        if ctx.needs_input_grad[0]:
            grad_inputs = grad_outputs.mm(ctx.features)

        # momentum update
        for x, y in zip(inputs, indexes):
            ctx.features[y] = ctx.momentum * ctx.features[y] + (1. - ctx.momentum) * x
            ctx.features[y] /= ctx.features[y].norm()

        return grad_inputs, None, None, None


def hm(inputs, indexes, features, momentum=0.5):
    return HM.apply(inputs, indexes, features, torch.Tensor([momentum]).to(inputs.device))


class HybridMemory(nn.Module):
    def __init__(self, num_features, num_samples, temp=0.05, momentum=0.2):
        super(HybridMemory, self).__init__()
        self.num_features = num_features
        self.num_samples = num_samples

        self.momentum = momentum
        self.temp = temp

        self.register_buffer('features', torch.zeros(num_samples, num_features))
        self.register_buffer('labels', torch.zeros(num_samples).long())

    def forward(self, inputs, another_inputs_full, indexes, back=False):
        # inputs: B*2048, features: L*2048
        old_inputs = inputs.clone()
        inputs = hm(inputs, indexes, self.features, self.momentum)
        inputs /= self.temp
        B = inputs.size(0)

        def masked_softmax(vec, mask, dim=1, epsilon=1e-6):
            exps = torch.exp(vec)
            masked_exps = exps * mask.float().clone()
            masked_sums = masked_exps.sum(dim, keepdim=True) + epsilon
            return (masked_exps / masked_sums)

        targets = self.labels[indexes].clone()
        labels = self.labels.clone()

        sim = torch.zeros(labels.max() + 1, B).float().cuda()
        sim.index_add_(0, labels, inputs.t().contiguous())
        nums = torch.zeros(labels.max() + 1, 1).float().cuda()
        nums.index_add_(0, labels, torch.ones(self.num_samples, 1).float().cuda())
        mask = (nums > 0).float()
        sim /= (mask * nums + (1 - mask)).clone().expand_as(sim)
        mask = mask.expand_as(sim)
        masked_sim = masked_softmax(sim.t().contiguous(), mask.t().contiguous())
        #
        if back == False:
            return F.nll_loss(torch.log(masked_sim + 1e-6), targets) + \
                   self.focal_loss(targets.detach().clone(), sim=sim.clone(), mask=mask.clone())
        else:
            return F.nll_loss(torch.log(masked_sim + 1e-6), targets) + \
                   self.focal_loss(targets.detach().clone(), sim=sim.clone(), mask=mask.clone()) + \
                   self.contrasloss(targets.detach().clone(), sim.clone(), mask.clone(), old_inputs.clone(),
                                    another_inputs_full.clone()) + \
                   self.contrasmemotyloss(targets.detach().clone(), sim.clone(), mask.clone(), old_inputs.clone(),
                                          another_inputs_full.clone())

    def focal_loss(self, targets, sim, mask):
        def masked_softmax(vec, mask, dim=1, epsilon=1e-6):
            exps = torch.exp(vec)
            masked_exps = exps * mask.float().clone()
            masked_sums = masked_exps.sum(dim, keepdim=True) + epsilon
            return (masked_exps / masked_sums)

        masked_sim = masked_softmax(sim.t().contiguous(), mask.t().contiguous())

        targets_onehot = torch.zeros(masked_sim.size()).cuda()
        targets_squeeze = torch.unsqueeze(targets, 1)
        targets_onehot.scatter_(1, targets_squeeze, float(1))

        target_ones_p = targets_onehot.clone()
        focal_p = target_ones_p.clone() * masked_sim.clone()  # pt
        focal_p_all = torch.pow(target_ones_p - focal_p, 4)  # 1-pt

        outputs = torch.log(masked_sim + 1e-6).float()
        loss = - (focal_p_all * outputs).float()
        loss = loss.sum(dim=1)
        loss = loss.mean(dim=0)
        return loss

    def contrasloss(self, targets, sim, mask, inputs, another_inputs):
        def masked_softmax(vec, mask, dim=1, epsilon=1e-6):
            exps = torch.exp(vec)
            masked_exps = exps * mask.float().clone()
            masked_sums = masked_exps.sum(dim, keepdim=True) + epsilon
            return (masked_exps / masked_sums)

        inputs = (inputs.t() / inputs.norm(dim=1)).t()
        another_inputs = (another_inputs.t() / another_inputs.norm(dim=1)).t()
        loss = -1 * (inputs * another_inputs).sum(dim=1).mean()
        return loss

    def contrasmemotyloss(self, targets, sim, mask, inputs_ins, another_inputs):
        def masked_softmax(vec, mask, dim=1, epsilon=1e-6):
            exps = torch.exp(vec)
            masked_exps = exps * mask.float().clone()
            masked_sums = masked_exps.sum(dim, keepdim=True) + epsilon
            return (masked_exps / masked_sums)

        labels = self.labels.clone()
        memory_dynamic = torch.zeros(labels.max() + 1, self.num_features).float().cuda()
        memory_dynamic.index_add_(0, labels, self.features.contiguous().clone())
        nums = torch.zeros(labels.max() + 1, 1).float().cuda()
        nums.index_add_(0, labels, torch.ones(self.num_samples, 1).float().cuda())
        mask = (nums > 0).float()
        memory_dynamic /= (mask * nums + (1 - mask)).clone().expand_as(memory_dynamic)
        inputs = torch.zeros(len(inputs_ins), self.num_features).cuda()
        for i in range(len(inputs)):
            inputs[i] = memory_dynamic[targets[i]]

        inputs = inputs.detach()

        # norm -x*y
        inputs = (inputs.t() / inputs.norm(dim=1)).t()
        another_inputs = (another_inputs.t() / another_inputs.norm(dim=1)).t()
        loss = -1 * (inputs * another_inputs).sum(dim=1).mean()
        return loss
