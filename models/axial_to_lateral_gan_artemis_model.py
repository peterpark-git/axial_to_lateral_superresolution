import torch
import itertools
import numpy as np

import util.util
from util.image_pool import ImagePool
from .base_model import BaseModel
from . import networks
from util.util import normalize
from torch.autograd import grad


class AxialToLateralGANArtemisModel(BaseModel):
    """
    This class implements the CycleGAN model with cubes, for learning image-to-image translation without paired data.

    The model takes a 3D image cube as an input and outputs a 3D image stack that correspond to the output cube.
    Note that the loss functions are readjusted for cube dataset.

    GAN Loss is calculated in 2D between axial image and lateral image. -> Discriminator takes 2D images
                                                                        -> Generator takes 3D images.

    This model is an updated version of Athena: instead of employing two axial discriminators, we replace one of them with
    projection discriminator, which compares a XY MIP with an axial MIP. Plus, we also add L1 loss for XY projection.
    We choose which axial plane (YZ or XZ) to use per iteration by chance (50%, 50%).

    G_A: original -> isotropic
    G_B: isotropic -> original

    D_A_axial: original_XY <-> isotropic_axial
    D_A_lateral: original_XY <-> isotropic_XY
    D_A_axial_proj: original_XY_MIP <-> isotropic_axial_MIP

    D_B_axial: original_axial <-> reconstructed_axial
    D_B_lateral: original_lateral <-> reconstructed_lateral
    D_B_axial_proj: original_axial_MIP <-> reconstructed_axial_MIP

    We only consider one path: A->B.
    We also do not consider buffering fake images for discriminator.

    CycleGAN paper: https://arxiv.org/pdf/1703.10593.pdf
    """

    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        parser.set_defaults(no_dropout=True)  # default CycleGAN did not use dropout
        if is_train:
            parser.add_argument('--lambda_A', type=float, default=10.0, help='weight for cycle loss (A -> B -> A)')
            parser.add_argument('--gan_mode', type=str, default='vanilla',
                                help='the type of GAN objective. [vanilla| lsgan | wgangp]. vanilla GAN loss is the cross-entropy objective used in the original GAN paper.')

            parser.add_argument('--lambda_lateralpreserve', type=float, default=0.01,
                                help='weight for preserving lateral projection information in (A->B path)')

            parser.add_argument('--lambda_plane', type=int, nargs='+', default=[1, 1, 1],
                                help='weight ratio for matching (target vs. target) and (target vs. source) and (MIP target vs. MIP source).')

            parser.add_argument('--mix_planes', action='store_true',
                                help='You mix the target planes for slicing and MIP: e.g. XZ slice and YZ MIP.')

        parser.add_argument('--netG_B', type=str, default='deep_linear_gen',
                            help='specify the generator in B->A path. ')
        parser.add_argument('--randomize_projection_depth', action='store_true', help='randomize the depth for MIP')
        parser.add_argument('--projection_depth', type=int, default=50,
                            help='depth for maximum intensity projections. ')
        # parser.set_defaults(norm='instance')

        return parser

    def __init__(self, opt):
        """Initialize the CycleGAN class.

        Parameters:
            opt (Option class)-- stores all the experiment flags; needs to be a subclass of BaseOptions
        """

        BaseModel.__init__(self, opt)
        # specify the training losses you want to print out. The training/test scripts will call <BaseModel.get_current_losses>
        self.loss_names = ['D_A_lateral', 'D_A_axial', 'D_A_axial_proj', 'G_A', 'G_A_lateral', 'G_A_axial',
                           'G_A_axial_proj', 'cycle',
                           'D_B_lateral', 'D_B_axial', 'D_B_axial_proj', 'G_B', 'G_B_lateral', 'G_B_axial',
                           'G_B_axial_proj', 'lateral_preserve']
        self.gan_mode = opt.gan_mode

        self.gen_dimension = 3  # 3D convolutions in generators
        self.dis_dimension = 2  # 2D convolutions in discriminators

        self.randomize_projection_depth = opt.randomize_projection_depth
        if not (self.randomize_projection_depth):
            self.projection_depth_custom = opt.projection_depth
        else:
            self.max_projection_depth = opt.projection_depth
            print("Projection depth is randomized with maximum depth of %d." % (self.max_projection_depth))

        # specify the images you want to save/display. The training/test scripts will call <BaseModel.get_current_visuals>
        visual_names_A = ['real', 'fake', 'rec']
        visual_names_B = ['real', 'fake', 'rec']

        self.lambda_plane_target, self.lambda_slice, self.lambda_proj = [
            factor / (opt.lambda_plane[0] + opt.lambda_plane[1] + opt.lambda_plane[2]) for factor in opt.lambda_plane]

        self.visual_names = visual_names_A + visual_names_B  # combine visualizations for A and B

        self.target_sl_axis = 0  # XY plane
        self.mix_planes = opt.mix_planes  # you mix the slicing plane for D_axial and the MIP plane for D_axial_proj (e.g. XZ and MIP YZ).

        # specify the models you want to save to the disk. The training/test scripts will call <BaseModel.save_networks> and <BaseModel.load_networks>.
        if self.isTrain:
            self.model_names = ['G_A', 'G_B', 'D_A_lateral', 'D_A_axial', 'D_A_axial_proj', 'D_B_lateral', 'D_B_axial',
                                'D_B_axial_proj']
        else:  # during test time, only load Gs
            self.model_names = ['G_A', 'G_B']

        # define networks (both Generators and discriminators)
        # The naming is different from those used in the paper.
        # Code (vs. paper): G_A (G), G_B (F), D_A (D_Y), D_B (D_X)
        self.netG_A = networks.define_G(opt.input_nc, opt.output_nc, opt.ngf, opt.netG, opt.norm,
                                        not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids,
                                        dimension=self.gen_dimension)

        self.netG_B = networks.define_G(opt.output_nc, opt.input_nc, opt.ngf, opt.netG_B, opt.norm,
                                        not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids,
                                        dimension=self.gen_dimension)

        if self.isTrain:  # define discriminators
            self.netD_A_axial = networks.define_D(opt.output_nc, opt.ndf, opt.netD,
                                                  opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, False,
                                                  self.gpu_ids, dimension=self.dis_dimension)

            self.netD_A_lateral = networks.define_D(opt.output_nc, opt.ndf, opt.netD,
                                                    opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, False,
                                                    self.gpu_ids, dimension=self.dis_dimension)

            self.netD_A_axial_proj = networks.define_D(opt.output_nc, opt.ndf, opt.netD,
                                                       opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, False,
                                                       self.gpu_ids, dimension=self.dis_dimension)

            self.netD_B_axial = networks.define_D(opt.input_nc, opt.ndf, opt.netD,
                                                  opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, False,
                                                  self.gpu_ids, dimension=self.dis_dimension)

            self.netD_B_lateral = networks.define_D(opt.input_nc, opt.ndf, opt.netD,
                                                    opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, False,
                                                    self.gpu_ids, dimension=self.dis_dimension)

            self.netD_B_axial_proj = networks.define_D(opt.input_nc, opt.ndf, opt.netD, opt.n_layers_D, opt.norm,
                                                       opt.init_type, opt.init_gain, False,
                                                       self.gpu_ids, dimension=self.dis_dimension)

        if self.isTrain:
            # define loss functions
            self.criterionGAN = networks.GANLoss(opt.gan_mode).to(self.device)  # define GAN loss.
            self.criterionCycle = torch.nn.L1Loss()
            self.criterionXYproj = torch.nn.L1Loss()

            # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
            self.optimizer_G = torch.optim.Adam(itertools.chain(self.netG_A.parameters(), self.netG_B.parameters()),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(
                itertools.chain(self.netD_A_axial.parameters(), self.netD_A_lateral.parameters(),
                                self.netD_A_axial_proj.parameters(),
                                self.netD_B_axial.parameters(), self.netD_B_lateral.parameters(),
                                self.netD_B_axial_proj.parameters()),
                lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)

        ## END OF INITIALIZATION ##

    def set_input(self, input):
        """Unpack input data from the dataloader and perform necessary pre-processing steps.

        Parameters:
            input (dict): include the data itself and its metadata information.

        The option 'direction' can be used to swap domain A and domain B.
        """
        AtoB = self.opt.direction == 'AtoB'
        self.real = input['A' if AtoB else 'B'].to(self.device)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']

        self.cube_shape = self.real.shape
        self.num_slice = self.cube_shape[-3]

        if not (self.randomize_projection_depth):
            self.projection_depth = self.projection_depth_custom
        else:
            self.projection_depth = np.random.randint(2, self.max_projection_depth + 1)

        # print (self.projection_depth)
        # real_lateral_proj = Volume(self.real, self.device).get_projection(self.projection_depth, 0)
        #
        # mip_np = util.util.tensor2im(real_lateral_proj, is_normalized=True, imtype=np.uint8).squeeze()
        # util.util.save_image(mip_np, '/mnt/trunk/test_real.png')

    def forward(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>.
        In this version, we iterate through each slice in a cube.
        """
        self.fake = self.netG_A(self.real)  # G_A(A)
        self.rec = self.netG_B(self.fake)  # G_B(G_A(A))

    def backward_D_basic(self, netD, real, fake, slice_axis_real, slice_axis_fake):

        """Calculate GAN loss for the discriminator

        Parameters:
            netD (network)      -- the discriminator D
            real (tensor array) -- real images
            fake (tensor array) -- images generated by a generator

        Return the discriminator loss.
        We also call loss_D.backward() to calculate the gradients.
        """

        # Real
        pred_real = self.iter_f(real, netD, slice_axis_real)
        pred_fake = self.iter_f(fake.detach(), netD, slice_axis_fake)

        # real
        loss_D_real = self.criterionGAN(pred_real, True)  # Target_is_real -> True: loss (pred_real - unit vector)

        # Fake
        loss_D_fake = self.criterionGAN(pred_fake, False)  # no loss with the unit vector

        # Combined loss and calculate gradients
        loss_D = (loss_D_real + loss_D_fake) * 0.5

        loss_D.backward()
        return loss_D

    def backward_D_projection(self, netD, real, fake, slice_axis_real, slice_axis_fake):

        """Calculate GAN loss for the discriminator

        Parameters:
            netD (network)      -- the discriminator D
            real (tensor array) -- real images
            fake (tensor array) -- images generated by a generator

        Return the discriminator loss.
        We also call loss_D.backward() to calculate the gradients.
        """
        # Real
        pred_real = self.proj_f(real, netD, slice_axis_real)
        pred_fake = self.proj_f(fake.detach(), netD, slice_axis_fake)

        # real
        loss_D_real = self.criterionGAN(pred_real, True)  # Target_is_real -> True: loss (pred_real - unit vector)

        # Fake
        loss_D_fake = self.criterionGAN(pred_fake, False)  # no loss with the unit vector

        # Combined loss and calculate gradients
        loss_D = (loss_D_real + loss_D_fake) * 0.5

        loss_D.backward()
        return loss_D

    def backward_D_A_lateral(self):
        self.loss_D_A_lateral = self.backward_D_basic(self.netD_A_lateral, self.real, self.fake, self.target_sl_axis,
                                                      self.target_sl_axis)  # comparing XY_original to XY_fake

    def backward_D_A_axial(self, source_sl_axis):
        """Calculate GAN loss for discriminator D_A"""
        self.loss_D_A_axial = self.backward_D_basic(self.netD_A_axial, self.real, self.fake, self.target_sl_axis,
                                                    source_sl_axis)  # comparing XY_original to YZ_fake

    def backward_D_A_axial_proj(self, source_sl_axis):
        if self.mix_planes:
            if source_sl_axis == 1:
                compare_sl_axis = 0
            else:
                compare_sl_axis = 1
        else:
            compare_sl_axis = source_sl_axis

        self.loss_D_A_axial_proj = self.backward_D_projection(self.netD_A_axial_proj, self.real, self.fake,
                                                              self.target_sl_axis,
                                                              compare_sl_axis)  # comparing XY_original to XZ_fake

    def backward_D_B_lateral(self):
        self.loss_D_B_lateral = self.backward_D_basic(self.netD_B_lateral, self.real, self.rec, self.target_sl_axis,
                                                      self.target_sl_axis)  # comparing XY_original to XY_reconstructed

    def backward_D_B_axial(self, source_sl_axis):
        """Calculate GAN loss for discriminator D_B, which compares the original and the reconstructed. """
        self.loss_D_B_axial = self.backward_D_basic(self.netD_B_axial, self.real, self.rec, source_sl_axis,
                                                    source_sl_axis)  # comparing YZ_original to YZ_reconstructed

    def backward_D_B_axial_proj(self, source_sl_axis):
        if self.mix_planes:
            if source_sl_axis == 1:
                compare_sl_axis = 0
            else:
                compare_sl_axis = 1
        else:
            compare_sl_axis = source_sl_axis

        self.loss_D_B_axial_proj = self.backward_D_projection(self.netD_B_axial_proj, self.real, self.rec,
                                                              compare_sl_axis,
                                                              compare_sl_axis)  # comparing XZ_original to XZ_reconstructed

    def backward_G(self, source_sl_axis):
        """Calculate the loss for generators G_A and G_B"""
        lambda_A = self.opt.lambda_A
        lambda_lateralpreserve = self.opt.lambda_lateralpreserve

        if self.mix_planes:
            if source_sl_axis == 1:
                remain_sl_axis = 0
            else:
                remain_sl_axis = 1
        else:
            remain_sl_axis = source_sl_axis

        self.loss_G_A_lateral = self.criterionGAN(self.iter_f(self.fake, self.netD_A_lateral, self.target_sl_axis),
                                                  True) * self.lambda_plane_target
        self.loss_G_A_axial = self.criterionGAN(self.iter_f(self.fake, self.netD_A_axial, source_sl_axis),
                                                True) * self.lambda_slice
        self.loss_G_A_axial_proj = self.criterionGAN(self.proj_f(self.fake, self.netD_A_axial_proj, remain_sl_axis),
                                                     True) * self.lambda_proj
        self.loss_G_A = self.loss_G_A_lateral + self.loss_G_A_axial + self.loss_G_A_axial_proj

        self.loss_G_B_lateral = self.criterionGAN(self.iter_f(self.rec, self.netD_B_lateral, self.target_sl_axis),
                                                  True) * self.lambda_plane_target
        self.loss_G_B_axial = self.criterionGAN(self.iter_f(self.rec, self.netD_B_axial, source_sl_axis),
                                                True) * self.lambda_slice
        self.loss_G_B_axial_proj = self.criterionGAN(self.proj_f(self.rec, self.netD_B_axial_proj, remain_sl_axis),
                                                     True) * self.lambda_proj
        self.loss_G_B = self.loss_G_B_lateral + self.loss_G_B_axial + self.loss_G_B_axial_proj

        # This model only includes forward cycle loss || G_B(G_A(A)) - A||
        self.loss_cycle = self.criterionCycle(self.rec, self.real) * lambda_A

        # This model keeps
        real_lateral_proj = Volume(self.real, self.device).get_projection(self.projection_depth,
                                                                          0)  #
        fake_lateral_proj = Volume(self.fake, self.device).get_projection(self.projection_depth, 0)

        self.loss_lateral_preserve = self.criterionXYproj(real_lateral_proj, fake_lateral_proj) * lambda_lateralpreserve

        # combined loss and calculate gradients
        self.loss_G = self.loss_G_A + self.loss_G_B + self.loss_cycle + self.loss_lateral_preserve
        self.loss_G.backward()

    def optimize_parameters(self):
        """Calculate losses, gradients, and update network weights; called in every training iteration"""
        # forward
        self.forward()  # compute fake images and reconstruction images.

        # randomly choose which axial plane to update.
        chance = np.random.uniform(0, 1)
        if chance < 0.5:
            source_sl_axis = 1
        else:
            source_sl_axis = 0

        # G_A and G_B
        self.set_requires_grad(
            [self.netD_A_lateral, self.netD_A_axial, self.netD_A_axial_proj, self.netD_B_lateral, self.netD_B_axial,
             self.netD_B_axial_proj], False)  # Ds require no gradients when optimizing Gs
        self.optimizer_G.zero_grad()  # set G_A and G_B's gradients to zero
        self.backward_G(source_sl_axis)  # calculate gradients for G_A and G_B
        self.optimizer_G.step()  # update G_A and G_B's weights

        # D_A and D_B
        self.set_requires_grad(
            [self.netD_A_lateral, self.netD_A_axial, self.netD_A_axial_proj, self.netD_B_lateral, self.netD_B_axial,
             self.netD_B_axial_proj], True)
        self.optimizer_D.zero_grad()  # set D_A and D_B's gradients to zero

        self.backward_D_A_lateral()
        self.backward_D_A_axial(source_sl_axis)  # calculate gradients for D_A's
        self.backward_D_A_axial_proj(source_sl_axis)

        self.backward_D_B_lateral()
        self.backward_D_B_axial(source_sl_axis)  # calculate gradients for D_B's
        self.backward_D_B_axial_proj(source_sl_axis)
        self.optimizer_D.step()  # update D_A and D_B's weights

    # Apply discriminator to each slice in a given dimension and save it as a volume.
    def iter_f(self, input, function, slice_axis):
        input_tensor = Volume(input, self.device)  # Dimension: batch, color_channel, z, y, x
        test_slice = function(
            input_tensor.get_slice(0, slice_axis))  # get image dimension after convolving through the discriminator
        output_tensor = Volume(
            torch.zeros(test_slice.shape[0], test_slice.shape[1], self.num_slice, test_slice.shape[2],
                        test_slice.shape[3]), self.device)

        for i in range(self.num_slice):
            input_slice = input_tensor.get_slice(i, slice_axis)  # batch, color_channel, y, x
            output_slice = function(input_slice)
            output_tensor.set_slice(i, 0, output_slice)  # stack the output by the discriminator on z-axis.

        return output_tensor.get_volume()

    def proj_f(self, input, function, slice_axis):
        input_volume = Volume(input, self.device)
        mip = input_volume.get_projection(self.projection_depth, slice_axis)
        output_mip = function(mip)
        return output_mip


class Volume():
    def __init__(self, vol, device):
        self.volume = vol.to(device)  # push the volume to cuda memory
        self.num_slice = vol.shape[-1]

    # returns a slice: # batch, color_channel, y, x
    def get_slice(self, slice_index, slice_axis, pick_random=False):

        if pick_random:
            slice_index_pick = np.random.randint(self.num_slice)
        else:
            slice_index_pick = slice_index

        if slice_axis == 0:
            return self.volume[:, :, slice_index_pick, :, :]

        elif slice_axis == 1:
            return self.volume[:, :, :, slice_index_pick, :]

        elif slice_axis == 2:
            return self.volume[:, :, :, :, slice_index_pick]

    def set_slice(self, slice_index, slice_axis, new_slice):
        if slice_axis == 0:
            self.volume[:, :, slice_index, :, :] = new_slice

        elif slice_axis == 1:
            self.volume[:, :, :, slice_index, :] = new_slice

        elif slice_axis == 2:
            self.volume[:, :, :, :, slice_index] = new_slice

    def get_projection(self, depth, slice_axis):
        start_index = np.random.randint(0, self.num_slice - depth)

        if slice_axis == 0:
            volume_ROI = self.volume[:, :, start_index:start_index + depth, :, :]

        elif slice_axis == 1:
            volume_ROI = self.volume[:, :, :, start_index:start_index + depth, :]

        elif slice_axis == 2:
            volume_ROI = self.volume[:, :, :, :, start_index:start_index + depth]

        mip = torch.max(volume_ROI, slice_axis + 2)[0]
        return mip

    def get_volume(self):
        return self.volume