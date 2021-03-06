from __future__ import print_function
import argparse
import os
import random
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
from torch.autograd import Variable
import numpy as np

from model import _netlocalD, _netG
from psnr import psnr

parser = argparse.ArgumentParser()
parser.add_argument('--dataset', default='lungs', help='streetview | tiny-imagenet | lungs ')
parser.add_argument('--dataroot', default='', help='path to dataset')
parser.add_argument('--workers', type=int, help='number of data loading workers', default=2)
parser.add_argument('--batchSize', type=int, default=64, help='input batch size')
parser.add_argument('--imageSize', type=int, default=128, help='the height / width of the input image to network')
parser.add_argument('--patchSize', type=int, default=64, help='the height / width of the patch to be reconstructed')
parser.add_argument('--beforeCropSize', type=int, default=128, help='the height / width of the patch to be reconstructed')

parser.add_argument('--nz', type=int, default=100, help='size of the latent z vector')
parser.add_argument('--ngf', type=int, default=64)
parser.add_argument('--ndf', type=int, default=64)
parser.add_argument('--nc', type=int, default=1)
parser.add_argument('--niter', type=int, default=1, help='number of epochs to train for')
parser.add_argument('--lr', type=float, default=0.0002, help='learning rate, default=0.0002')
parser.add_argument('--beta1', type=float, default=0.5, help='beta1 for adam. default=0.5')
parser.add_argument('--cuda', action='store_true', help='enables cuda')
parser.add_argument('--ngpu', type=int, default=1, help='number of GPUs to use')
parser.add_argument('--netG', default='model/netG_streetview.pth', help="path to netG (to continue training)")
parser.add_argument('--netD', default='model/netlocalD.pth', help="path to netD (to continue training)")
parser.add_argument('--outf', default='.', help='folder to output images and model checkpoints')
parser.add_argument('--manualSeed', type=int, help='manual seed')

parser.add_argument('--nBottleneck', type=int, default=4000, help='of dim for bottleneck of encoder')
parser.add_argument('--overlapPred', type=int, default=4, help='overlapping edges')
parser.add_argument('--nef', type=int, default=64, help='of encoder filters in first conv layer')
parser.add_argument('--wtl2', type=float, default=0.998, help='0 means do not use else use with this weight')
parser.add_argument('--wtlD', type=float, default=0.001, help='0 means do not use else use with this weight')

opt = parser.parse_args()
opt.cuda = True

opt.ndf = 128  # Discriminator
# opt.nef = 128  # Generator
LIMIT_SAMPLES = 1  # Number of sample minibatches to reconstruct. Set to -1 to use all test set

print(opt)

try:
    os.makedirs('predict/' + str(opt.dataset))
except OSError:
    pass

if opt.manualSeed is None:
    opt.manualSeed = 1234  # random.randint(1, 10000)

random.seed(opt.manualSeed)
torch.manual_seed(opt.manualSeed)
if opt.cuda:
    torch.cuda.manual_seed_all(opt.manualSeed)

cudnn.benchmark = True

if torch.cuda.is_available() and not opt.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

if opt.dataset == 'tiny-imagenet':
    # folder dataset
    dataset = dset.ImageFolder(root='dataset_tiny_imagenet/test',
                               transform=transforms.Compose([
                                   transforms.Resize(opt.imageSize),
                                   transforms.CenterCrop(opt.imageSize),
                                   transforms.ToTensor(),
                                   # transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                               ]))
elif opt.dataset == 'lungs':
    # folder dataset
    if opt.nc == 1:
        transform = transforms.Compose([
            
            transforms.Grayscale(),
            transforms.Resize(opt.imageSize),
            transforms.CenterCrop(opt.imageSize),
            transforms.ToTensor()
        ])
    else:
        transform = transforms.Compose([
            transforms.Resize(opt.imageSize),
            transforms.CenterCrop(opt.imageSize),
            transforms.ToTensor()
        ])
    dataset = dset.ImageFolder(root='dataset_lungs/test_64', transform=transform)
elif opt.dataset == 'streetview':
    transform = transforms.Compose([transforms.Resize(opt.imageSize),
                                    transforms.CenterCrop(opt.imageSize),
                                    transforms.ToTensor(),
                                    transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])
    dataset = dset.ImageFolder(root="dataset/val", transform=transform)

assert dataset
dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batchSize,
                                         shuffle=False, num_workers=int(opt.workers))

ngpu = int(opt.ngpu)
nz = int(opt.nz)
ngf = int(opt.ngf)
ndf = int(opt.ndf)
nc = 3
nef = int(opt.nef)
nBottleneck = int(opt.nBottleneck)
wtl2 = float(opt.wtl2)
overlapL2Weight = 10


# custom weights initialization called on netG and netD
def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


resume_epoch = 0

netG = _netG(opt)
netG.apply(weights_init)
if opt.netG != '':
    netG.load_state_dict(torch.load(opt.netG, map_location=lambda storage, location: storage)['state_dict'])
    resume_epoch = torch.load(opt.netG)['epoch']
print(netG)

netD = _netlocalD(opt)
netD.apply(weights_init)
if opt.netD != '':
    netD.load_state_dict(torch.load(opt.netD, map_location=lambda storage, location: storage)['state_dict'])
    resume_epoch = torch.load(opt.netD)['epoch']
print(netD)

print("This model was trained for ", resume_epoch, "epochs.")
resume_epoch = 0

criterion = nn.BCELoss()
criterionMSE = nn.MSELoss()

input_real = torch.FloatTensor(opt.batchSize, 3, opt.imageSize, opt.imageSize)
input_cropped = torch.FloatTensor(opt.batchSize, 3, opt.imageSize, opt.imageSize)
label = torch.FloatTensor(opt.batchSize)
real_label = 1
fake_label = 0

real_center = torch.FloatTensor(opt.batchSize, 3, int(opt.imageSize / 2), int(opt.imageSize / 2))

if opt.cuda:
    netD.cuda()
    netG.cuda()
    criterion.cuda()
    criterionMSE.cuda()
    input_real, input_cropped, label = input_real.cuda(), input_cropped.cuda(), label.cuda()
    real_center = real_center.cuda()

input_real = Variable(input_real)
input_cropped = Variable(input_cropped)
label = Variable(label)

real_center = Variable(real_center)

# setup optimizer
optimizerD = optim.Adam(netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
optimizerG = optim.Adam(netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))

for epoch in range(resume_epoch, opt.niter):
    for i, data in enumerate(dataloader, 0):
        if LIMIT_SAMPLES == -1 or i < LIMIT_SAMPLES:
            real_cpu, _ = data
            real_center_cpu = real_cpu[:, :, int(opt.imageSize / 4):int(opt.imageSize / 4) + int(opt.imageSize / 2),
                              int(opt.imageSize / 4):int(opt.imageSize / 4) + int(opt.imageSize / 2)]
            batch_size = real_cpu.size(0)
            input_real.data.resize_(real_cpu.size()).copy_(real_cpu)
            input_cropped.data.resize_(real_cpu.size()).copy_(real_cpu)
            real_center.data.resize_(real_center_cpu.size()).copy_(real_center_cpu)
            input_cropped.data[:, 0,
            int(opt.imageSize / 4 + opt.overlapPred):int(opt.imageSize / 4 + opt.imageSize / 2 - opt.overlapPred),
            int(opt.imageSize / 4 + opt.overlapPred):int(
                opt.imageSize / 4 + opt.imageSize / 2 - opt.overlapPred)] = 2 * 117.0 / 255.0 - 1.0
            if opt.nc > 1:
                input_cropped.data[:, 1,
                int(opt.imageSize / 4 + opt.overlapPred):int(opt.imageSize / 4 + opt.imageSize / 2 - opt.overlapPred),
                int(opt.imageSize / 4 + opt.overlapPred):int(
                    opt.imageSize / 4 + opt.imageSize / 2 - opt.overlapPred)] = 2 * 104.0 / 255.0 - 1.0
                input_cropped.data[:, 2,
                int(opt.imageSize / 4 + opt.overlapPred):int(opt.imageSize / 4 + opt.imageSize / 2 - opt.overlapPred),
                int(opt.imageSize / 4 + opt.overlapPred):int(
                    opt.imageSize / 4 + opt.imageSize / 2 - opt.overlapPred)] = 2 * 123.0 / 255.0 - 1.0
            
            # train with real
            netD.zero_grad()
            label.data.resize_(batch_size, 1).fill_(real_label)
            
            # input("Proceed..." + str(real_center.data.size()))
            
            output = netD(real_center)
            errD_real = criterion(output, label)
            # errD_real.backward()
            D_x = output.data.mean()
            
            # train with fake
            # noise.data.resize_(batch_size, nz, 1, 1)
            # noise.data.normal_(0, 1)
            fake = netG(input_cropped)
            label.data.fill_(fake_label)
            output = netD(fake.detach())
            # print(output.data.size(), " ", label.data.size())
            # input("")
            errD_fake = criterion(output, label)
            # errD_fake.backward()
            D_G_z1 = output.data.mean()
            errD = errD_real + errD_fake
            # optimizerD.step()
            
            ############################
            # (2) Update G network: maximize log(D(G(z)))
            ###########################
            netG.zero_grad()
            label.data.fill_(real_label)  # fake labels are real for generator cost
            output = netD(fake)
            errG_D = criterion(output, label)
            # errG_D.backward(retain_variables=True)
            
            # errG_l2 = criterionMSE(fake,real_center)
            wtl2Matrix = real_center.clone()
            wtl2Matrix.data.fill_(wtl2 * overlapL2Weight)
            wtl2Matrix.data[:, :, int(opt.overlapPred):int(opt.imageSize / 2 - opt.overlapPred),
            int(opt.overlapPred):int(opt.imageSize / 2 - opt.overlapPred)] = wtl2
            
            errG_l2 = (fake - real_center).pow(2)
            errG_l2 = errG_l2 * wtl2Matrix
            errG_l2 = errG_l2.mean()
            
            errG = (1 - wtl2) * errG_D + wtl2 * errG_l2
            
            # errG.backward()
            
            D_G_z2 = output.data.mean()
            # optimizerG.step()
            
            print('[%d/%d] Loss_D: %.4f Loss_G: %.4f / %.4f l_D(x): %.4f l_D(G(z)): %.4f'
                  % (i + 1, LIMIT_SAMPLES,
                     errD.data[0], errG_D.data[0], errG_l2.data[0], D_x, D_G_z1,))
            
            vutils.save_image(real_cpu,
                              'predict/' + str(opt.dataset) + '/' + str(i) + '_real.png')
            recon_image = input_cropped.clone()
            recon_image.data[:, :,
            int(opt.imageSize / 2 - opt.patchSize / 2):int(opt.imageSize / 2 + opt.patchSize / 2),
            int(opt.imageSize / 2 - opt.patchSize / 2):int(opt.imageSize / 2 + opt.patchSize / 2)] = fake.data
            vutils.save_image(recon_image.data,
                              'predict/' + str(opt.dataset) + '/' + str(i) + '_recon.png')
            
            # Compute PSNR
            
            # t = real_center_np - fake_np
            # l2 = np.mean(np.square(t))
            # print(l2)
            # l1 = np.mean(np.abs(t))
            # print(l1)
            
            
            real_center_np = (real_center.data.cpu().numpy() + 1) * 127.5
            fake_np = (fake.data.cpu().numpy() + 1) * 127.5
            real_cpu_np = (real_cpu.cpu().numpy() + 1) * 127.5
            recon_image_np = (recon_image.data.cpu().numpy() + 1) * 127.5
            
            p = 0
            total_p = 0
            for j in range(opt.batchSize):
                p += psnr(real_center_np[j].transpose(1, 2, 0), fake_np[j].transpose(1, 2, 0))
                total_p += psnr(real_cpu_np[j].transpose(1, 2, 0), recon_image_np[j].transpose(1, 2, 0))
            print("\t  PSNR per Patch: ", p / opt.batchSize)
            print("\t  PSNR per Image: ", total_p / opt.batchSize)
        
        
        else:
            break
