"""
1.cell的输出是concat还是最后一个节点？目前是设置为最后一个节点，
但是加入新的cell时，新cell的输入可以是任意前驱节点，这导致加入cell前后的不一致？
2.当前cell，只允许连接到前面4个节点
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from operations import *
from torch.autograd import Variable
from genotypes import PRIMITIVES
from genotypes import Genotype


class MixedOp(nn.Module):

    def __init__(self, C, stride):
        super(MixedOp, self).__init__()
        self._ops = nn.ModuleList()
        for primitive in PRIMITIVES:
            op = OPS[primitive](C, stride, False)
            if 'pool' in primitive:
                op = nn.Sequential(op, nn.BatchNorm2d(C, affine=False))
                # op = nn.Sequential(op)
            self._ops.append(op)

    def forward(self, x, weights):
        return sum(w * op(x) for w, op in zip(weights, self._ops))


class Cell(nn.Module):

    def __init__(self, steps, genotypes, C_prev_prev, C_prev, C):
        super(Cell, self).__init__()
        # genotypes如果存储dim信息，是不是可以解决问题？
        # if not genotypes:
        #     self.preprocess0 = ReLUConvBN(C_prev_prev, C, 1, 1, 0, affine=False)
        #     self.preprocess1 = ReLUConvBN(C_prev, C, 1, 1, 0, affine=False)

        self._genotypes = genotypes  # learned cells
        self._steps = steps  # 每次加入两个节点
        self._ops = nn.ModuleList()
        self._bns = nn.ModuleList()
        if not self._genotypes:  # 这里有没有可能出错？？？
            n = 2
        else:
            n = 4
        for i in range(self._steps):
            for j in range(n + i):
                stride = 1
                op = MixedOp(C, stride)
                self._ops.append(op)

    def forward(self, states, weights):
        # s0 = self.preprocess0(states[0])
        # s1 = self.preprocess1(states[1])
        offset = 0
        states = states[-4:]  # 只要最近的4个父亲节点
        for i in range(self._steps):
            s = sum(self._ops[offset + j](h, weights[offset + j]) for j, h in enumerate(states))
            offset += len(states)
            states.append(s)

        # return torch.cat(states[-2:], dim=1)  # 这里有点问题吧？cat起来之后，dim就变了啊？
        return states[-2], states[-1]


class Learned_Cell(nn.Module):

    def __init__(self, genotype, C_prev_prev, C_prev, C):
        super(Learned_Cell, self).__init__()
        # self.preprocess0 = ReLUConvBN(C_prev_prev, C, 1, 1, 0)
        # self.preprocess1 = ReLUConvBN(C_prev, C, 1, 1, 0)
        op_names, indices = zip(*genotype.normal)
        self._compile(C, op_names, indices)

    def _compile(self, C, op_names, indices):
        assert len(op_names) == len(indices)
        self._steps = len(op_names) // 2

        self._ops = nn.ModuleList()
        for name, index in zip(op_names, indices):
            stride = 1
            op = OPS[name](C, stride, True)
            self._ops += [op]
        self._indices = indices

    def forward(self, states, drop_prob=0.0):
        # new_states = []
        # for i in range(len(states)):
        #     new_states.append(self.preprocess_list[i](states[i]))  # 可能会导致参数特别多。。
        # states = [s0, s1]
        for i in range(self._steps):  # 这个_steps改成啥？？？
            h1 = states[self._indices[2 * i]]
            h2 = states[self._indices[2 * i + 1]]
            op1 = self._ops[2 * i]
            op2 = self._ops[2 * i + 1]
            h1 = op1(h1)
            h2 = op2(h2)
            s = h1 + h2
            states += [s]
        # return torch.cat([states[i] for i in self._concat], dim=1)
        return states


class Network(nn.Module):

    def __init__(self, genotypes, C, num_classes, layers, criterion, steps=2, stem_multiplier=3, temperature=5.0):
        super(Network, self).__init__()
        # 当前的network，是不是应该继承之前的arch参数和weight参数？并且weight参数只要subnet的部分？
        # 那还不如直接传入base arch和对应的weight参数？subnet weights怎么获得？
        self._C = C
        self._num_classes = num_classes
        self._layers = layers
        self._criterion = criterion
        self._steps = steps  # 每个grow step加入两个节点
        self._genotypes = genotypes
        self.pre_nodes = 4
        stem_multiplier = 1

        C_curr = stem_multiplier * C  # stem_multiplier应该是根据经验取值的
        self.stem = nn.Sequential(
            nn.Conv2d(3, C_curr, 3, padding=1, bias=False),
            nn.BatchNorm2d(C_curr)
        )
        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C

        self.pre_cells = nn.ModuleList()
        for i in range(len(genotypes)):
            if i == 0:  # 用于preprocess的处理
                self.pre_cells.append(Learned_Cell(genotypes[i], C_prev_prev, C_prev, C_curr).cuda())
            else:  # 为啥这里必须要加上cuda()？
                self.pre_cells.append(Learned_Cell(genotypes[i], C_curr, C_curr, C_curr).cuda())

        self.cell = Cell(steps, genotypes, C_prev_prev, C_prev, C_curr)

        self.global_pooling = nn.AdaptiveAvgPool2d(1)  # 这里应该用convnet中的reshape代替吗？
        self.classifier = nn.Linear(C, num_classes)

        self._initialize_alphas()

    # def new(self):
    #   model_new = Network(self._C, self._num_classes, self._layers, self._criterion).cuda()
    #   for x, y in zip(model_new.arch_parameters(), self.arch_parameters()):
    #       x.data.copy_(y.data)
    #   return model_new

    def forward(self, input):
        s0 = s1 = self.stem(input)
        states = [s0, s1]
        for pre_cell in self.pre_cells:
            states = pre_cell(states)  # 这里检查一下有没有错误。。应该没问题

        weights = F.softmax(self.alphas_normal, dim=-1)
        s0_curr, s1_curr = self.cell(states, weights)
        # s_curr = self.cell(states, weights)

        out = self.global_pooling(s1_curr)  # 最终的输出是第二个节点的输出？或者是最后两个节点的平均值？还是搞成可选的？
        logits = self.classifier(out.view(out.size(0), -1))  # 输出层改成convnet形式？

        return logits

    def _loss(self, input, target):
        logits = self(input)
        return self._criterion(logits, target)

    def _initialize_alphas(self):
        k = sum(1 for i in range(self._steps) for n in range(2 + 2 * len(self._genotypes) + i))
        k2 = sum(1 for i in range(self._steps) for n in range(self.pre_nodes + i))
        k = k2 if k > k2 else k

        num_ops = len(PRIMITIVES)
        self.alphas_normal = Variable(1e-3 * torch.randn(k, num_ops).cuda(), requires_grad=True)
        self._arch_parameters = [
            self.alphas_normal
        ]

    def arch_parameters(self):
        return self._arch_parameters

    def genotype(self):

        def _parse(weights):
            gene = []
            if not self._genotypes:  # 第一个cell，只有两个可选的输入节点
                n = 2
                offset = 2
            else:
                n = self.pre_nodes
                offset = self.pre_nodes
            start = 0
            for i in range(self._steps):
                end = start + n
                W = weights[start:end].copy()
                edges = sorted(range(i + offset),
                               key=lambda x: -max(W[x][k] for k in range(len(W[x])) if k != PRIMITIVES.index('none')))[:2]

                for j in edges:
                    k_best = None
                    for k in range(len(W[j])):
                        if k != PRIMITIVES.index('none'):
                            if k_best is None or W[j][k] > W[j][k_best]:
                                k_best = k
                    if len(self._genotypes) >= 2:
                        gene.append((PRIMITIVES[k_best], j + 2 * (len(self._genotypes) - 1)))
                    else:
                        gene.append((PRIMITIVES[k_best], j))
                start = end
                n += 1
            return gene

        print(F.softmax(self.alphas_normal, dim=-1))
        gene_normal = _parse(F.softmax(self.alphas_normal, dim=-1).data.cpu().numpy())

        genotype = Genotype(normal=gene_normal)
        # concat = range(2 + self._steps - self._multiplier, self._steps + 2)
        # genotype = Genotype(normal=gene_normal, normal_concat=concat)  # normal_concat是不是不需要了？？？
        return genotype


class SubNetwork(nn.Module):

    def __init__(self, C, num_classes, layers, genotypes):
        super(SubNetwork, self).__init__()
        self._layers = layers

        stem_multiplier = 1
        C_curr = stem_multiplier * C
        self.stem = nn.Sequential(
            nn.Conv2d(3, C_curr, 3, padding=1, bias=False),
            nn.BatchNorm2d(C_curr)
        )

        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C
        self.pre_cells = nn.ModuleList()
        for i in range(len(genotypes)):
            cell = Learned_Cell(genotypes[i], C_prev_prev, C_prev, C_curr)
            self.pre_cells += [cell]

        # if auxiliary:
        #     self.auxiliary_head = AuxiliaryHeadCIFAR(C_to_auxiliary, num_classes)
        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        self.subnet_classifier = nn.Linear(C_prev, num_classes)

    def forward(self, input):
        s0 = s1 = self.stem(input)
        states = [s0, s1]
        for i, cell in enumerate(self.pre_cells):
            states = cell(states)
            # if i == 2 * self._layers // 3:  # 啥意思啊？？？
            #     if self._auxiliary and self.training:
            #         logits_aux = self.auxiliary_head(s1)

        s1 = states[-1]
        out = self.global_pooling(s1)
        logits = self.subnet_classifier(out.view(out.size(0), -1))
        return logits
