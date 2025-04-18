import os
import math
import argparse
import random
import logging
import time
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from data.data_sampler import DistIterSampler

import options.options as option
from utils import util
from data import create_dataloader, create_dataset
from models import create_model

import data.util as data_util
import cv2
import numpy as np
import skimage.metrics as sm
import csv
from tqdm import tqdm

def init_dist(backend='nccl', **kwargs):
    ''' initialization for distributed training'''
    # if mp.get_start_method(allow_none=True) is None:
    if mp.get_start_method(allow_none=True) != 'spawn':
        mp.set_start_method('spawn')
    rank = int(os.environ['RANK'])
    num_gpus = torch.cuda.device_count()
    torch.cuda.set_device(rank % num_gpus)
    dist.init_process_group(backend=backend, **kwargs)

def main():
    #### options
    parser = argparse.ArgumentParser()
    parser.add_argument('-opt', type=str, help='Path to option YAML file.')
    parser.add_argument('--launcher', choices=['none', 'pytorch'], default='none',
                        help='job launcher')
    parser.add_argument('--local_rank', type=int, default=0)
    args = parser.parse_args()

    #MAKR# Load Yaml Configuration
    opt = option.parse(args.opt, is_train=True)
    
    #### distributed training settings
    if args.launcher == 'none':  # disabled distributed training
        opt['dist'] = False
        rank = -1
        print('Disabled distributed training.')
    else:
        opt['dist'] = True
        init_dist()
        world_size = torch.distributed.get_world_size()
        rank = torch.distributed.get_rank()

    #MARK# Load Pretrained Model
    #### loading resume state if exists
    if opt['path'].get('resume_state', None):
        # distributed resuming: all load into default GPU
        device_id = torch.cuda.current_device()
        resume_state = torch.load(opt['path']['resume_state'], map_location=lambda storage, loc: storage.cuda(device_id))
        option.check_resume(opt, resume_state['iter'])  # check resume options
    else:
        resume_state = None

    #MARK# Init Logger
    #### mkdir and loggers
    if rank <= 0:  # normal training (rank -1) OR distributed training (rank 0)
        if resume_state is None:
            util.mkdir_and_rename(
                opt['path']['experiments_root'])  # rename experiment folder if exists
            util.mkdirs((path for key, path in opt['path'].items() if not key == 'experiments_root'
                         and 'pretrain_model' not in key and 'resume' not in key))

        # config loggers. Before it, the log will not work
        util.setup_logger('base', opt['path']['log'], 'train_' + opt['name'], level=logging.INFO,
                          screen=True, tofile=True)
        logger = logging.getLogger('base')
        logger.info(option.dict2str(opt))
        # tensorboard logger
        if opt['use_tb_logger'] and 'debug' not in opt['name']:
            version = float(torch.__version__[0:3])
            if version >= 1.1:  # PyTorch 1.1
                from torch.utils.tensorboard import SummaryWriter
            else:
                logger.info(
                    'You are using PyTorch {}. Tensorboard will use [tensorboardX]'.format(version))
                from tensorboardX import SummaryWriter
            tb_logger = SummaryWriter(log_dir='../tb_logger/' + opt['name'])
    else:
        util.setup_logger('base', opt['path']['log'], 'train', level=logging.INFO, screen=True)
        logger = logging.getLogger('base')

    # convert to NoneDict, which returns None for missing keys
    opt = option.dict_to_nonedict(opt)

    #MARK# IXI or BrainTS, for the two datasets
    which_model = opt['mode']

    #MARK# Seed, fucking useless
    #### random seed
    seed = opt['train']['manual_seed']
    if seed is None:
        seed = random.randint(1, 10000)
    if rank <= 0:
        logger.info('Random seed: {}'.format(seed))
    util.set_random_seed(seed)

    torch.backends.cudnn.benckmark = True
    # torch.backends.cudnn.deterministic = True

    #### create train and val dataloader
    dataset_ratio = 1.0
    # dataset_ratio = 200  # enlarge the size of each epoch
    for phase, dataset_opt in opt['datasets'].items():
        if phase == 'train':
            train_set = create_dataset(dataset_opt, train=True)
            train_size = int(math.ceil(len(train_set) / dataset_opt['batch_size'])) # Iter number per epoch
            total_iters = int(opt['train']['niter'])
            total_epochs = int(math.ceil(total_iters / train_size))
            if opt['dist']:
                train_sampler = DistIterSampler(train_set, world_size, rank, dataset_ratio)
                total_epochs = int(math.ceil(total_iters / (train_size * dataset_ratio)))
            else:
                train_sampler = None
            train_loader = create_dataloader(train_set, dataset_opt, opt, train_sampler)
            if rank <= 0:
                logger.info('Number of train images: {:,d}, iters: {:,d}'.format(
                    len(train_set), train_size))
                logger.info('Total epochs needed: {:d} for iters {:,d}'.format(
                    total_epochs, total_iters))
        
        elif phase == 'val':
            val_set = create_dataset(dataset_opt, train=False)
            val_loader = create_dataloader(val_set, dataset_opt, opt, None)
            if rank <= 0:
                logger.info('Number of val images: {:d}'.format(len(val_set)))
        else:
            raise NotImplementedError('Phase [{:s}] is not recognized.'.format(phase))
    assert train_loader is not None
    assert val_loader is not None

    #### create model
    #MAKR# What the fuck is the two mode
    model = create_model(opt)

    #### resume training
    if resume_state:
        logger.info('Resuming training from epoch: {}, iter: {}.'.format(
            resume_state['epoch'], resume_state['iter']))

        start_epoch = resume_state['epoch'] + 1
        current_step = resume_state['iter']
        model.resume_training(resume_state)  # handle optimizers and schedulers
    else:
        current_step = 0
        start_epoch = 0

    #### training
    best_performance = 0
    logger.info('Start training from epoch: {:d}, iter: {:d}'.format(start_epoch, current_step))
    for epoch in range(start_epoch, total_epochs + 1):
        if opt['dist']:
            train_sampler.set_epoch(epoch)
        for _, train_data in enumerate(train_loader):
            current_step += 1
            if current_step > total_iters:
                break
                        #### training
            model.feed_data(train_data)
            model.optimize_parameters()

            #### log
            if current_step % opt['logger']['print_freq'] == 0:
                logs = model.get_current_log()
                message = '<epoch:{:3d}, iter:{:8,d}, lr:('.format(epoch, current_step)
                for v in model.get_current_learning_rate():
                    message += '{:.3e},'.format(v)
                message += ')>'
                for k, v in logs.items():
                    message += '{:s}: {:.4e} '.format(k, v)
                    # tensorboard logger
                    if opt['use_tb_logger'] and 'debug' not in opt['name']:
                        if rank <= 0:
                            tb_logger.add_scalar(k, v, current_step)
                if rank <= 0:
                    logger.info(message)
            #### save models and training states
            if rank <= 0:
                if current_step % opt['logger']['save_checkpoint_freq'] == 0:
                    logger.info('Saving models and training states at the end of step: ' + str(current_step))
                    model.save(current_step)
                    model.save_training_state(epoch, current_step)

            #### update learning rate
            model.update_learning_rate(current_step+1, warmup_iter=opt['train']['warmup_iter'])
            #### validation
            if opt['model'] == 'joint-rec':
                if current_step % opt["train"]["val_freq"] == 0 and rank <= 0:
                    avg_psnr_im1 = 0.0
                    avg_psnr_im2 = 0.0
                    idx = 0
                    for val_data in tqdm(val_loader): 
                        model.feed_data(val_data)
                        model.test()
                        
                        visuals = model.get_current_visuals()
                        img_num = visuals["im1_restore"].shape[0]
                        for i in range(img_num):
                            sr_img_1 = visuals["im1_restore"][i, 0, :, :]  # (1, w, h)
                            gt_img_1 = visuals["im1_GT"][i, 0, :, :]  # (1, w, h) 
                            sr_img_2 = visuals["im2_restore"][i, 0, :, :]  # (1, w, h)
                            gt_img_2 = visuals["im2_GT"][i, 0, :, :]  # (1, w, h)                  
                            # calculate PSNR
                            cur_psnr_im1 = util.calculate_psnr(sr_img_1.numpy()*255., gt_img_1.numpy()*255.)
                            avg_psnr_im1 += cur_psnr_im1
                            cur_psnr_im2 = util.calculate_psnr(sr_img_2.numpy()*255., gt_img_2.numpy()*255.)
                            avg_psnr_im2 += cur_psnr_im2

                        idx += img_num   
                    avg_psnr_im1 = avg_psnr_im1 / idx
                    avg_psnr_im2 = avg_psnr_im2 / idx
                    # log
                    logger.info("# image1 Validation # PSNR: {:.6f}".format(avg_psnr_im1))
                    logger.info("# image2 Validation # PSNR: {:.6f}".format(avg_psnr_im2))
            else:
                if current_step % opt["train"]["val_freq"] == 0 and rank <= 0:
                    avg_psnr_im1 = 0.0
                    idx = 0
                    for val_data in tqdm(val_loader): 
                        model.feed_data(val_data)
                        model.test()
            
                        visuals = model.get_current_visuals()
                        img_num = visuals["im1_GT"].shape[0]
                        # print(visuals["im1_restore"].shape, visuals["im1_GT"].shape)
                        for i in range(img_num):
                            sr_img_1 = visuals["im1_restore"][i, 0, :, :]  # (1, w, h)
                            gt_img_1 = visuals["im1_GT"][i, 0, :, :]  # (1, w, h) 
                        
                            #MARK# Metrics
                            # calculate PSNR
                            if which_model == 'IXI' or which_model == 'brain' or which_model == 'knee':
                                cur_psnr_im1 = util.calculate_psnr(sr_img_1.numpy()*255., gt_img_1.numpy()*255.)
                            elif which_model == 'fastmri':
                                cur_psnr_im1 = util.calculate_psnr_fastmri(gt_img_1.numpy(), sr_img_1.numpy())
                        
                            avg_psnr_im1 += cur_psnr_im1

                        idx += img_num   
                    avg_psnr_im1 = avg_psnr_im1 / idx
                    # log
                    logger.info("# image1 Validation # PSNR: {:.6f}".format(avg_psnr_im1))


    if rank <= 0:
        logger.info('Saving the final model.')
        model.save('latest')
        logger.info('End of training.')


if __name__ == '__main__':
    main()
