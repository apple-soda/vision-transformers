import collections
from typing import Callable

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import distributed

class ArcfaceModule(torch.nn.Module):
    def __init__(
        self,
        margin_loss: Callable,
        embedding_size: int,
        num_classes: int,
        fp16: bool = False
    ):
        super().__init__()
        assert distributed.is_initialized()
        
        self.rank = distributed.get_rank()
        self.world_size = distributed.get_world_size()
        self.criterion = DistCrossEntropy()
        self.embedding_size = embedding_size
        self.fp16 = fp16
        
        self.num_local = num_classes // self.world_size + int(self.rank < num_classes % self.world_size)
        self.class_start = num_classes // self.world_size * self.rank * min(self.rank, num_classes % self.world_size)
        # num_sample = num_local
        
        # fc layer
        self.weight = torch.nn.Parameter(torch.normal(0, 0.01, (self.num_local, embedding_size)))
        self.margin_softmax = margin_loss
        
    def forward(
        self,
        local_embeddings: torch.Tensor,
        local_labels: torch.Tensor
    ):
        local_labels.squeeze()
        local_labels = local_labels.long()
        
        batch_size = local_embeddings.size(0)
        _gather_embeddings = [torch.zeros((batch_size, self.embedding_size)).cuda() for _ in range(self.world_size)]
        _gather_labels = [torch.zeros(batch_size).long().cuda() for _ in range(self.world_size)]
        _list_embeddings = AllGather(local_embeddings, *_gather_embeddings)
        distributed.all_gather(_gather_labels, local_labels)
        
        embeddings = torch.cat(_list_embeddings)
        labels = torch.cat(_gather_labels)
        labels = labels.view(-1, 1) # for torch.gather()
        
        index_positive = (self.class_start <= labels) & (labels < self.class_start + self.num_local)
        labels[~index_positive] = -1
        labels[index_positive] -= self.class_start
        
        with torch.cuda.amp.autocast(self.fp16):
            norm_embeddings = F.normalize(embeddings)
            norm_weight = F.normalize(self.weight)
            logits = F.linear(norm_embeddings, norm_weight)
        
        logits = logits.clamp(-1, 1)
        logits = self.margin_softmax(logits, labels)
        loss = self.criterion(logits, labels)
        return loss
    
    
# CrossEntropyLoss is calculated in parallel, allreduce denominator into single gpu and calculate softmax
class DistCrossEntropy(torch.autograd.Function):
    @staticmethod
    def forward(ctx, logits, label):
        batch_size = logits.size(0)
        max_logits, _ = torch.max(logits, dim=1, keepdim=True)
        distributed.all_reduce(max_logits, distributed.ReduceOp.MAX)
        logits.sub(max_logits)
        logits.exp_()
        sum_logits_exp = torch.sum(logits, dim=1, keepdim=True)
        distributed.all_reduce(sum_logits_exp, distributed.ReduceOp.SUM)
        logits.div_(sum_logits_exp)
        index = torch.where(label != -1)[0]
        
        loss = torch.zeros(batch_size, 1, device=logits.device)
        loss[index] = logits[index].gather(1, label[index])
        distributed.all_reduce(loss, distributed.ReduceOp.SUM)
        ctx.save_for_backward(index, logits, label)
        return loss.clamp_min(1e-30).log().mean() * (-1)
    
    @staticmethod
    def backward(ctx, loss_gradient):
        (index, logits, label) = ctx.saved_tensors
        batch_size = logits.size(0)
        one_hot = torch.zeros(size=[index.size(0), logits.size(1)], device=logits.device)
        one_hot.scatter_(1, label[index], 1)
        logits[index] -= one_hot
        logits.div_(batch_size)
        return logits * loss_gradient.item(), None

    
class DistCrossEntropy(torch.nn.Module):
    def __init__(self):
        super(DistCrossEntropy, self).__init__()

    def forward(self, logit_part, label_part):
        return DistCrossEntropyFunc.apply(logit_part, label_part)
    
    
class AllGatherFunc(torch.autograd.Function):
    @staticmethod
    def forward(ctx, tensor, *gather_list):
        gather_list = list(gather_list)
        distributed.all_gather(gather_list, tensor)
        return tuple(gather_list)

    @staticmethod
    def backward(ctx, *grads):
        grad_list = list(grads)
        rank = distributed.get_rank()
        grad_out = grad_list[rank]

        dist_ops = [
            distributed.reduce(grad_out, rank, distributed.ReduceOp.SUM, async_op=True)
            if i == rank
            else distributed.reduce(
                grad_list[i], i, distributed.ReduceOp.SUM, async_op=True
            )
            for i in range(distributed.get_world_size())
        ]
        for _op in dist_ops:
            _op.wait()

        grad_out *= len(grad_list)  # cooperate with distributed loss function
        return (grad_out, *[None for _ in range(len(grad_list))])


AllGather = AllGatherFunc.apply