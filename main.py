# @Author  : Sky chen
# @Email   : dzhchxk@126.com
# @Personal homepage  : https://coderskychen.cn

try:
    import tensorflow as tf
except ImportError:
    print("Tensorflow not installed; No tensorboard logging.")
    tf = None

import argparse
import os
import time
import shutil
import torch
import torchvision
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
from torch.nn.utils import clip_grad_norm

from dataloader.dataset import *
from models import *
from dataloader.transforms import *
from opts import parser


def add_summary_value(writer, key, value, iteration):
    summary = tf.Summary(value=[tf.Summary.Value(tag=key, simple_value=value)])
    writer.add_summary(summary, iteration)


def return_ucfdataset_path(modality):
    '''
    for split 1
    :param modality: 
    :return: 
    '''
    filename_categories = '/home/mcg/cxk/dataset/UCF101-frames-TSN/category.txt'
    root_data = '/home/mcg/cxk/dataset/UCF101-frames-TSN'
    filename_imglist_train = '/home/mcg/cxk/dataset/UCF101-frames-TSN/trainlist_mid01.txt'
    filename_imglist_val = '/home/mcg/cxk/dataset/UCF101-frames-TSN/testlist_mid01.txt'
    if modality == 'RGB':
        prefix = 'img_{:05d}.jpg'
    else:
        prefix = 'flow_{:s}_{:05d}.jpg'

    with open(filename_categories) as f:
        lines = f.readlines()
    categories = [item.rstrip() for item in lines]
    return categories, filename_imglist_train, filename_imglist_val, root_data, prefix


best_prec1 = 0


def main():
    torch.backends.cudnn.enabled = False

    global args, best_prec1
    args = parser.parse_args()
    assert len(args.train_id) > 0

    check_rootfolders(args.train_id)
    summary_w = tf and tf.summary.FileWriter(os.path.join('results', args.train_id, args.root_log))  #tensorboard

    categories, args.train_list, args.val_list, args.root_path, prefix = return_ucfdataset_path(args.modality)
    num_class = len(categories)

    args.store_name = '_'.join([args.model, args.modality, args.arch])
    print('storing name: ' + args.store_name)

    policies = -1

    if args.model == 'TSN':
        model = TSN(num_class, args.num_segments, args.modality,
                          base_model=args.arch, dropout=args.dropout,
                          partial_bn=not args.no_partialbn)
        policies = model.get_optim_policies()
    elif args.model == 'TRN':
        model = TRN(num_class, args.num_segments, args.modality,
                          base_model=args.arch, dropout=args.dropout,
                          partial_bn=not args.no_partialbn)
        policies = model.get_optim_policies()
    elif args.model == 'ECO':
        model = ECO(num_class, args.num_segments, args.modality,
                          base_model=args.arch, dropout=args.dropout,
                          partial_bn=not args.no_partialbn, type='lite')
        policies = model.get_optim_policies()
    else:
        model = []
        print('error!')
        exit()

    print('Nums of Params:', get_number_of_param(model))

    crop_size = model.crop_size
    scale_size = model.scale_size
    input_mean = model.input_mean
    input_std = model.input_std
    train_augmentation = model.get_augmentation()

    model = torch.nn.DataParallel(model, device_ids=args.gpus).cuda()

    if args.resume:
        if os.path.isfile(args.resume):
            print(("=> loading checkpoint '{}'".format(args.resume)))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            print(("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.evaluate, checkpoint['epoch'])))
        else:
            print(("=> no checkpoint found at '{}'".format(args.resume)))

    # Data loading code
    if args.modality != 'RGBDiff' and input_mean[0] != -1:
        normalize = GroupNormalize(input_mean, input_std)
    else:
        normalize = IdentityTransform()

    if args.modality == 'RGB':
        data_length = 1
    elif args.modality in ['Flow', 'RGBDiff']:
        data_length = 5

    if args.modality == 'RGB' and (args.model == 'C3D' or args.model == 'I3D'):
        data_length = 16  # clip length

    if args.model == 'TSN' or args.model == 'TRN' or args.model == 'ECO':
        datasettrain = TSNDataSet(args.root_path, args.train_list, args.num_segments,
                                        new_length=data_length,
                                        modality=args.modality,
                                        image_tmpl=prefix,
                                        transform=torchvision.transforms.Compose([
                                            train_augmentation,
                                            Stack(roll=(args.arch in ['BNInception', 'BNInception_3c', 'InceptionV3'])),
                                            ToTorchFormatTensor(div=(args.arch not in ['BNInception', 'BNInception_3c', 'InceptionV3'])),
                                            normalize,
                                        ]))

        datasetval = TSNDataSet(args.root_path, args.val_list, args.num_segments,
                                      new_length=data_length,
                                      modality=args.modality,
                                      image_tmpl=prefix,
                                      transform=torchvision.transforms.Compose([
                                          GroupScale(int(scale_size)),
                                          GroupCenterCrop(crop_size),
                                          Stack(roll=(args.arch in ['BNInception', 'BNInception_3c', 'InceptionV3'])),
                                          ToTorchFormatTensor(div=(args.arch not in ['BNInception', 'BNInception_3c', 'InceptionV3'])),
                                          normalize,
                                      ]))

    trainvidnum = len(datasettrain)

    train_loader = torch.utils.data.DataLoader(
        datasettrain,
        batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True)

    val_loader = torch.utils.data.DataLoader(
        datasetval,
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    # define loss function (criterion) and optimizer
    criterion = torch.nn.CrossEntropyLoss().cuda()

    if policies != -1:
        for group in policies:
            print(('group: {} has {} params, lr_mult: {}, decay_mult: {}'.format(
                group['name'], len(group['params']), group['lr_mult'], group['decay_mult'])))

        optimizer = torch.optim.SGD(policies, args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    else:
        optimizer = torch.optim.SGD(model.parameters(), args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    for epoch in range(args.start_epoch, args.epochs):
        adjust_learning_rate(optimizer, epoch, args.lr_steps, args.factor, policies != -1)

        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, trainvidnum, summary_w)

        # evaluate on validation set
        if (epoch + 1) % args.eval_freq == 0 or epoch == args.epochs - 1:
            prec1 = validate(val_loader, model, criterion, (epoch + 1) * trainvidnum, summary_w)

            # remember best prec@1 and save checkpoint
            is_best = prec1 > best_prec1
            best_prec1 = max(prec1, best_prec1)
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
            }, is_best)


def train(train_loader, model, criterion, optimizer, epoch, vidnums, summary_w):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    if args.no_partialbn:
        model.module.partialBN(False)
    else:
        # model.partialBN(True)
        model.module.partialBN(True)

    # switch to train mode
    model.train()

    samples_have_seen = epoch*vidnums

    end = time.time()
    for i, (input, target) in enumerate(train_loader):
        # if i>5:
        #     break
        # measure data loading time
        data_time.update(time.time() - end)

        target = target.cuda(async=True)

        input_var = torch.autograd.Variable(input)
        target_var = torch.autograd.Variable(target)

        bs = input_var.size(0)

        # compute output
        output = model(input_var)
        loss = criterion(output, target_var)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1,5))
        losses.update(loss.data[0], input.size(0))
        top1.update(prec1[0], input.size(0))
        top5.update(prec5[0], input.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()

        loss.backward()

        if args.clip_gradient is not None:
            total_norm = clip_grad_norm(model.parameters(), args.clip_gradient)
            if total_norm > args.clip_gradient:
                print("clipping gradient: {} with coef {}".format(total_norm, args.clip_gradient / total_norm))

        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        samples_have_seen += bs

        if i % args.print_freq == 0:
            output = ('Epoch: [{0}][{1}/{2}], lr: {lr:.5f}\t'
                    'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                    'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                    'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                    'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                    'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                        epoch, i, len(train_loader), batch_time=batch_time,
                        data_time=data_time, loss=losses, top1=top1, top5=top5, lr=optimizer.param_groups[-1]['lr']))
            print(output)
            add_summary_value(summary_w, 'train_loss', losses.val, samples_have_seen)
            add_summary_value(summary_w, 'train_Prec@1', top1.val, samples_have_seen)
            add_summary_value(summary_w, 'train_Prec@5', top5.val, samples_have_seen)
            add_summary_value(summary_w, 'train_Prec@1_mean', top1.avg, samples_have_seen)
            add_summary_value(summary_w, 'train_Prec@5_mean', top5.avg, samples_have_seen)
            add_summary_value(summary_w, 'lr', optimizer.param_groups[-1]['lr'], samples_have_seen)


def validate(val_loader, model, criterion, iter, summary_w):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    for i, (input, target) in enumerate(val_loader):
        # if i>5:
        #     break
        target = target.cuda(async=True)
        input_var = torch.autograd.Variable(input, volatile=True)
        target_var = torch.autograd.Variable(target, volatile=True)

        # compute output
        output = model(input_var)
        loss = criterion(output, target_var)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1,5))

        losses.update(loss.data[0], input.size(0))
        top1.update(prec1[0], input.size(0))
        top5.update(prec5[0], input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        
        if i % args.print_freq == 0:
            output = ('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                   i, len(val_loader), batch_time=batch_time, loss=losses,
                   top1=top1, top5=top5))
            print(output)            
            # log.write(output + '\n')
            # log.flush()

    output = ('Testing Results: Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f} Loss {loss.avg:.5f}'
          .format(top1=top1, top5=top5, loss=losses))
    print(output)

    add_summary_value(summary_w, 'val_loss', losses.avg, iter)
    add_summary_value(summary_w, 'val_Prec@1', top1.avg, iter)
    add_summary_value(summary_w, 'val_Prec@5', top5.avg, iter)
    
    output_best = '\nBest Prec@1: %.3f'%(best_prec1)
    print(output_best)
    # log.write(output + ' ' + output_best + '\n')
    # log.flush()

    return top1.avg


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, './results/%s/%s/%s_checkpoint.pth.tar' % (args.train_id, args.root_model, args.store_name))
    if is_best:
        shutil.copyfile('./results/%s/%s/%s_checkpoint.pth.tar' % (args.train_id, args.root_model, args.store_name), './results/%s/%s/%s_best.pth.tar' % (args.train_id, args.root_model, args.store_name))

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch, lr_steps, factor, with_police=True):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    decay = factor ** (sum(epoch >= np.array(lr_steps)))
    lr = args.lr * decay
    decay = args.weight_decay
    if with_police:
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr * param_group['lr_mult']
            param_group['weight_decay'] = decay * param_group['decay_mult']
    else:
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
            param_group['weight_decay'] = decay


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

def check_rootfolders(trainid):
    """Create log and model folder"""
    folders_util = [args.root_log, args.root_model, args.root_output]
    if not os.path.exists('./results'):
        os.makedirs('./results')
    for folder in folders_util:
        if not os.path.exists(os.path.join('./results', trainid, folder)):
            print('creating folder ' + folder)
            os.makedirs(os.path.join('./results', trainid, folder))

if __name__ == '__main__':
    main()
