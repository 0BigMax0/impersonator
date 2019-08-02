import os
import torch
import torch.nn.functional as F
import numpy as np
from .models import BaseModel
from networks.networks import NetworksFactory, HumanModelRecovery
# from utils.nmr import SMPLRenderer
from utils.nmr_v2 import SMPLRenderer
from utils.detectors import PersonMaskRCNNDetector
import utils.cv_utils as cv_utils
import utils.util as util

import ipdb
from tqdm import tqdm


class Imitator(BaseModel):
    def __init__(self, opt):
        super(Imitator, self).__init__(opt)
        self._name = 'Imitator'

        self._create_networks()

        # prefetch variables
        self.src_info = None
        self.tsf_info = None
        self.T = None
        self.first_cam = None

    def _create_networks(self):
        # 0. create generator
        self.generator = self._create_generator().cuda()

        # 0. create bgnet
        if self._opt.bg_model:
            self.bgnet = self._create_bgnet().cuda()
        else:
            self.bgnet = self.generator.bg_model

        # 2. create hmr
        self.hmr = self._create_hmr().cuda()

        # 3. create render
        self.render = SMPLRenderer(image_size=self._opt.image_size, tex_size=self._opt.tex_size,
                                   has_front=self._opt.front_warp, fill_back=False).cuda()
        # 4. pre-processor
        if self._opt.has_detector:
            self.detector = PersonMaskRCNNDetector(ks=self._opt.bg_ks, threshold=0.5, to_gpu=True)
        else:
            self.detector = None

    def _create_bgnet(self):
        net = NetworksFactory.get_by_name('inpaintor', c_dim=4)
        self._load_params(net, self._opt.bg_model, need_module=False)
        net.eval()
        return net

    def _create_generator(self):
        net = NetworksFactory.get_by_name(self._opt.gen_name, bg_dim=4, src_dim=3 + self._G_cond_nc,
                                          tsf_dim=3 + self._G_cond_nc, repeat_num=self._opt.repeat_num)

        if self._opt.load_path:
            self._load_params(net, self._opt.load_path)
        elif self._opt.load_epoch > 0:
            self._load_network(net, 'G', self._opt.load_epoch)
        else:
            raise ValueError('load_path {} is empty and load_epoch {} is 0'.format(
                self._opt.load_path, self._opt.load_epoch))

        net.eval()
        return net

    def _create_hmr(self):
        hmr = HumanModelRecovery(self._opt.smpl_model)
        saved_data = torch.load(self._opt.hmr_model)
        hmr.load_state_dict(saved_data)
        hmr.eval()
        return hmr

    def visualize(self, *args, **kwargs):
        visualizer = args[0]
        if visualizer is not None:
            for key, value in kwargs.items():
                visualizer.vis_named_img(key, value)

    @torch.no_grad()
    def personalize(self, src_path, src_smpl=None, output_path='', visualizer=None):

        ori_img = cv_utils.read_cv2_img(src_path)

        # resize image and convert the color space from [0, 255] to [-1, 1]
        img = cv_utils.transform_img(ori_img, self._opt.image_size, transpose=True) * 2 - 1.0
        img = torch.tensor(img, dtype=torch.float32).cuda()[None, ...]

        if src_smpl is None:
            img_hmr = cv_utils.transform_img(ori_img, 224, transpose=True) * 2 - 1.0
            img_hmr = torch.tensor(img_hmr, dtype=torch.float32).cuda()[None, ...]
            src_smpl = self.hmr(img_hmr)
        else:
            src_smpl = torch.tensor(src_smpl, dtype=torch.float32).cuda()[None, ...]

        # source process, {'theta', 'cam', 'pose', 'shape', 'verts', 'j2d', 'j3d'}
        src_info = self.hmr.get_details(src_smpl)
        src_f2verts, src_fim, src_wim = self.render.render_fim_wim(src_info['cam'], src_info['verts'])
        # src_f2pts = src_f2verts[:, :, :, 0:2]
        src_info['fim'] = src_fim
        src_info['wim'] = src_wim
        src_info['cond'], _ = self.render.encode_fim(src_info['cam'], src_info['verts'], fim=src_fim, transpose=True)
        src_info['f2verts'] = src_f2verts
        src_info['p2verts'] = src_f2verts[:, :, :, 0:2]
        src_info['p2verts'][:, :, :, 1] *= -1

        if self._opt.only_vis:
            src_info['p2verts'] = self.render.get_vis_f2pts(src_info['p2verts'], src_fim)
        # add image to source info
        src_info['img'] = img
        src_info['image'] = ori_img

        # 2. process the src inputs
        if self.detector is not None:
            bbox, body_mask = self.detector.inference(img[0])
            bg_mask = 1 - body_mask
        else:
            bg_mask = util.morph(src_info['cond'][:, -1:, :, :], ks=self._opt.bg_ks, mode='erode')
            body_mask = 1 - bg_mask

        if self._opt.bg_model:
            src_info['bg'] = self.bgnet(img, masks=body_mask, only_x=True)
        else:
            incomp_img = img * bg_mask
            bg_inputs = torch.cat([incomp_img, bg_mask], dim=1)
            img_bg = self.bgnet(bg_inputs)
            src_info['bg_inputs'] = bg_inputs
            src_info['bg'] = bg_inputs[:, 0:3] + img_bg * bg_inputs[:, -1:]

        ft_mask = 1 - util.morph(src_info['cond'][:, -1:, :, :], ks=self._opt.ft_ks, mode='erode')
        src_inputs = torch.cat([img * ft_mask, src_info['cond']], dim=1)

        src_info['feats'] = self.generator.encode_src(src_inputs)

        self.src_info = src_info

        if visualizer is not None:
            visualizer.vis_named_img('src', img)
            visualizer.vis_named_img('bg', src_info['bg'])

        if output_path:
            cv_utils.save_cv2_img(src_info['image'], output_path, image_size=self._opt.image_size)

    @torch.no_grad()
    def _extract_smpls(self, input_file):
        img = cv_utils.read_cv2_img(input_file)
        img = cv_utils.transform_img(img, image_size=224) * 2 - 1.0  # hmr receive [-1, 1]
        img = img.transpose((2, 0, 1))
        img = torch.tensor(img, dtype=torch.float32).cuda()[None, ...]
        theta = self.hmr(img)[-1]

        return theta

    @torch.no_grad()
    def inference(self, tgt_paths, tgt_smpls=None, cam_strategy='smooth', output_dir='', visualizer=None):
        length = len(tgt_paths)

        outputs = []
        for t in range(length):
            tgt_path = tgt_paths[t]
            tgt_smpl = tgt_smpls[t] if tgt_smpls is not None else None

            tsf_inputs = self.transfer_params(tgt_path, tgt_smpl, cam_strategy, t=t)
            preds = self.forward(tsf_inputs, self.T, visualizer=visualizer)

            if visualizer is not None:
                gt = cv_utils.transform_img(self.tsf_info['image'], image_size=self._opt.image_size, transpose=True)
                visualizer.vis_named_img('pred_' + cam_strategy, preds)
                visualizer.vis_named_img('gt', gt[None, ...], denormalize=False)

            preds = preds[0].permute(1, 2, 0)
            preds = preds.cpu().numpy()
            outputs.append(preds)

            if output_dir:
                filename = os.path.split(tgt_path)[-1]

                cv_utils.save_cv2_img(preds, os.path.join(output_dir, 'pred_' + filename), normalize=True)
                cv_utils.save_cv2_img(self.tsf_info['image'], os.path.join(output_dir, 'gt_' + filename),
                                      image_size=self._opt.image_size)
            print('{} / {}'.format(t, length))

        return outputs

    @torch.no_grad()
    def inference_by_smpls(self, tgt_smpls, cam_strategy='smooth', output_dir='', visualizer=None):
        length = len(tgt_smpls)

        outputs = []
        for t in tqdm(range(length)):
            tgt_smpl = tgt_smpls[t] if tgt_smpls is not None else None

            tsf_inputs = self.transfer_params_by_smpl(tgt_smpl, cam_strategy, t=t)
            preds = self.forward(tsf_inputs, self.T, visualizer=visualizer)

            if visualizer is not None:
                gt = cv_utils.transform_img(self.tsf_info['image'], image_size=self._opt.image_size, transpose=True)
                visualizer.vis_named_img('pred_' + cam_strategy, preds)
                visualizer.vis_named_img('gt', gt[None, ...], denormalize=False)

            preds = preds[0].permute(1, 2, 0)
            preds = preds.cpu().numpy()
            outputs.append(preds)

            if output_dir:
                cv_utils.save_cv2_img(preds, os.path.join(output_dir, 'pred_%.8d.jpg' % t), normalize=True)

        return outputs

    @torch.no_grad()
    def transfer_params_by_smpl(self, tgt_smpl, cam_strategy='smooth', t=0):
        # get source info
        src_info = self.src_info
        tgt_smpl = torch.tensor(tgt_smpl, dtype=torch.float32).cuda()[None, ...]

        if t == 0 and cam_strategy == 'smooth':
            self.first_cam = tgt_smpl[:, 0:3].clone()

        # get transfer smpl
        tsf_smpl = self.swap_smpl(src_info['cam'], src_info['shape'], tgt_smpl, cam_strategy=cam_strategy)
        # transfer process, {'theta', 'cam', 'pose', 'shape', 'verts', 'j2d', 'j3d'}
        tsf_info = self.hmr.get_details(tsf_smpl)

        tsf_f2verts, tsf_fim, tsf_wim = self.render.render_fim_wim(tsf_info['cam'], tsf_info['verts'])
        # src_f2pts = src_f2verts[:, :, :, 0:2]
        tsf_info['fim'] = tsf_fim
        tsf_info['wim'] = tsf_wim
        tsf_info['cond'], _ = self.render.encode_fim(tsf_info['cam'], tsf_info['verts'], fim=tsf_fim, transpose=True)
        # tsf_info['sil'] = util.morph((tsf_fim != -1).float(), ks=self._opt.ft_ks, mode='dilate')

        T = self.render.cal_bc_transform(src_info['p2verts'], tsf_fim, tsf_wim)
        tsf_img = F.grid_sample(src_info['img'], T)
        tsf_inputs = torch.cat([tsf_img, tsf_info['cond']], dim=1)

        # add target image to tsf info
        tsf_info['tsf_img'] = tsf_img
        tsf_info['T'] = T

        self.T = T
        self.tsf_info = tsf_info

        return tsf_inputs

    @torch.no_grad()
    def swap_smpl(self, src_cam, src_shape, tgt_smpl, cam_strategy='smooth'):
        tgt_cam = tgt_smpl[:, 0:3].contiguous()
        pose = tgt_smpl[:, 3:75].contiguous()

        # TODO, need more tricky ways
        if cam_strategy == 'smooth':

            cam = src_cam.clone()
            delta_xy = tgt_cam[:, 1:] - self.first_cam[:, 1:]
            cam[:, 1:] += delta_xy

        elif cam_strategy == 'source':
            cam = src_cam
        else:
            cam = tgt_cam

        tsf_smpl = torch.cat([cam, pose, src_shape], dim=1)

        return tsf_smpl

    @torch.no_grad()
    def transfer_params(self, tgt_path, tgt_smpl=None, cam_strategy='smooth', t=0):
        # get source info
        src_info = self.src_info

        ori_img = cv_utils.read_cv2_img(tgt_path)
        if tgt_smpl is None:
            img_hmr = cv_utils.transform_img(ori_img, 224, transpose=True) * 2 - 1.0
            img_hmr = torch.tensor(img_hmr, dtype=torch.float32).cuda()[None, ...]
            tgt_smpl = self.hmr(img_hmr)
        else:
            tgt_smpl = torch.tensor(tgt_smpl, dtype=torch.float32).cuda()[None, ...]

        if t == 0 and cam_strategy == 'smooth':
            self.first_cam = tgt_smpl[:, 0:3].clone()

        # get transfer smpl
        tsf_smpl = self.swap_smpl(src_info['cam'], src_info['shape'], tgt_smpl, cam_strategy=cam_strategy)
        # transfer process, {'theta', 'cam', 'pose', 'shape', 'verts', 'j2d', 'j3d'}
        tsf_info = self.hmr.get_details(tsf_smpl)

        tsf_f2verts, tsf_fim, tsf_wim = self.render.render_fim_wim(tsf_info['cam'], tsf_info['verts'])
        # src_f2pts = src_f2verts[:, :, :, 0:2]
        tsf_info['fim'] = tsf_fim
        tsf_info['wim'] = tsf_wim
        tsf_info['cond'], _ = self.render.encode_fim(tsf_info['cam'], tsf_info['verts'], fim=tsf_fim, transpose=True)
        # tsf_info['sil'] = util.morph((tsf_fim != -1).float(), ks=self._opt.ft_ks, mode='dilate')

        T = self.render.cal_bc_transform(src_info['p2verts'], tsf_fim, tsf_wim)
        tsf_img = F.grid_sample(src_info['img'], T)
        tsf_inputs = torch.cat([tsf_img, tsf_info['cond']], dim=1)

        # add target image to tsf info
        tsf_info['tsf_img'] = tsf_img
        tsf_info['image'] = ori_img
        tsf_info['T'] = T

        self.T = T
        self.tsf_info = tsf_info

        return tsf_inputs

    def forward(self, tsf_inputs, T, visualizer=None):
        bg_img = self.src_info['bg']
        src_encoder_outs, src_resnet_outs = self.src_info['feats']

        tsf_color, tsf_mask = self.generator.inference(src_encoder_outs, src_resnet_outs, tsf_inputs, T)
        pred_imgs = tsf_mask * bg_img + (1 - tsf_mask) * tsf_color

        if self._opt.front_warp:
            pred_imgs = self.warp_front(pred_imgs, tsf_mask)

        # if visualizer is not None:
        #     visualizer.vis_named_img('tsf_mask', tsf_mask)
        #     visualizer.vis_named_img('tsf_color', tsf_color)

        return pred_imgs

    def warp_front(self, preds, mask):
        front_mask = self.render.encode_front_fim(self.tsf_info['fim'], transpose=True, front_fn=True)
        preds = (1 - front_mask) * preds + self.tsf_info['tsf_img'] * front_mask * (1 - mask)
        # preds = torch.clamp(preds + self.tsf_info['tsf_img'] * front_mask, -1, 1)
        return preds

    def pose_background(self, visualizer):
        """
            The idea borrows from Deep Image Prior.
        Args:
            visualizer:

        Returns:

        """
        import networks.losses as losses

        def fill_noise(x, noise_type):
            """Fills tensor `x` with noise of type `noise_type`."""
            if noise_type == 'u':
                x.uniform_()
            elif noise_type == 'n':
                x.normal_()
            else:
                assert False

        def get_noise(input_depth, method, spatial_size, noise_type='u', var=1. / 10):
            """Returns a pytorch.Tensor of size (1 x `input_depth` x `spatial_size[0]` x `spatial_size[1]`)
            initialized in a specific way.
            Args:
                input_depth: number of channels in the tensor
                method: `noise` for fillting tensor with noise; `meshgrid` for np.meshgrid
                spatial_size: spatial size of the tensor to initialize
                noise_type: 'u' for uniform; 'n' for normal
                var: a factor, a noise will be multiplicated by. Basically it is standard deviation scaler.
            """
            if isinstance(spatial_size, int):
                spatial_size = (spatial_size, spatial_size)
            if method == 'noise':
                shape = [1, input_depth, spatial_size[0], spatial_size[1]]
                net_input = torch.zeros(shape)

                fill_noise(net_input, noise_type)
                net_input *= var
            elif method == 'meshgrid':
                assert input_depth == 2
                X, Y = np.meshgrid(np.arange(0, spatial_size[1]) / float(spatial_size[1] - 1),
                                   np.arange(0, spatial_size[0]) / float(spatial_size[0] - 1))
                meshgrid = np.concatenate([X[None, :], Y[None, :]])
                net_input = torch.tensor(meshgrid).float()
            else:
                assert False

            return net_input

        def print_losses(*args, **kwargs):

            print('step = {}'.format(kwargs['step']))
            for key, value in kwargs.items():
                if key == 'epoch' or key == 'step':
                    continue
                print('\t{}, {:.6f}'.format(key, value.item()))

        # def compute_tv(mat):
        #     return torch.mean((mat[:, :, :, :-1] - mat[:, :, :, 1:]) ** 2) + \
        #            torch.mean((mat[:, :, :-1, :] - mat[:, :, 1:, :]) ** 2)

        def compute_tv(mat):
            return torch.mean(torch.abs(mat[:, :, :, :-1] - mat[:, :, :, 1:])) + \
                   torch.mean(torch.abs(mat[:, :, :-1, :] - mat[:, :, 1:, :]))

        init_lr = 0.001
        num_iters = 10000
        optimizer = torch.optim.Adam(self.bgnet.parameters(), lr=init_lr, betas=(0.5, 0.999))
        mse = torch.nn.MSELoss()

        feat_extractor = losses.vgg_loss.Vgg19(before_relu=False, get_x=False).cuda()
        pct_cri = losses.PerceptualLoss(weight=1.0, feat_extractors=feat_extractor)

        incomp_imgs = self.src_info['bg_inputs'][:, 0:3].detach()
        vis_masks = 1 - self.src_info['bg_inputs'][:, 3:].detach()
        inputs = get_noise(input_depth=4, method='noise', noise_type='n', spatial_size=vis_masks.shape[2:]).cuda()

        for step in range(num_iters):
            # inputs = get_noise(input_depth=4, method='noise', spatial_size=vis_masks.shape[2:]).cuda()
            out = self.bgnet(inputs)
            mse_loss = mse(out * vis_masks, incomp_imgs)
            tv_loss = compute_tv(out)
            loss = mse_loss + 0.001 * tv_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if step % 10 == 0:
                print_losses(step=step, total=loss, mse_loss=mse_loss, tv_loss=tv_loss)
                self.visualize(visualizer, incomp_imgs=incomp_imgs, comp_imgs=out)

        self.generator.eval()

    def post_personalize(self, out_dir, data_loader, visualizer):
        from networks.networks import FaceLoss

        bg_inpaint = self.src_info['bg']

        @torch.no_grad()
        def set_gen_inputs(sample):
            j2ds = sample['j2d'].cuda()  # (N, 4)
            T = sample['T'].cuda()  # (N, h, w, 2)
            T_cycle = sample['T_cycle'].cuda()  # (N, h, w, 2)
            T_cycle_vis = sample['T_cycle_vis'].cuda()  # (N, h, w, 2)
            bg_inputs = sample['bg_inputs'].cuda()  # (N, 4, h, w)
            src_inputs = sample['src_inputs'].cuda()  # (N, 6, h, w)
            tsf_inputs = sample['tsf_inputs'].cuda()  # (N, 6, h, w)
            src_fim = sample['src_fim'].cuda()
            tsf_fim = sample['tsf_fim'].cuda()
            init_preds = sample['preds'].cuda()
            images = sample['images']
            images = torch.cat([images[:, 0, ...], images[:, 1, ...]], dim=0).cuda()  # (2N, 3, h, w)
            pseudo_masks = sample['pseudo_masks']
            pseudo_masks = torch.cat([pseudo_masks[:, 0, ...], pseudo_masks[:, 1, ...]],
                                     dim=0).cuda()  # (2N, 1, h, w)

            return src_fim, tsf_fim, j2ds, T, T_cycle, T_cycle_vis, bg_inputs, \
                   src_inputs, tsf_inputs, images, init_preds, pseudo_masks

        def set_cycle_inputs(fake_tsf_imgs, src_inputs, tsf_inputs, T_cycle):
            # set cycle bg inputs
            tsf_bg_mask = tsf_inputs[:, -1:, ...]
            cycle_bg_inputs = torch.cat([fake_tsf_imgs * (1 - tsf_bg_mask), tsf_bg_mask], dim=1)

            # set cycle src inputs
            cycle_src_inputs = torch.cat([fake_tsf_imgs * tsf_bg_mask, tsf_inputs[:, 3:]], dim=1)

            # set cycle tsf inputs
            cycle_tsf_img = F.grid_sample(fake_tsf_imgs, T_cycle)
            cycle_tsf_inputs = torch.cat([cycle_tsf_img, src_inputs[:, 3:]], dim=1)

            return cycle_bg_inputs, cycle_src_inputs, cycle_tsf_inputs

        def warp(preds, tsf, fim, fake_tsf_mask):
            front_mask = self.render.encode_front_fim(fim, transpose=True)
            preds = (1 - front_mask) * preds + tsf * front_mask * (1 - fake_tsf_mask)
            # preds = torch.clamp(preds + tsf * front_mask, -1, 1)
            return preds

        def inference(bg_inputs, src_inputs, tsf_inputs, T, T_cycle, src_fim, tsf_fim):
            fake_bg, fake_src_color, fake_src_mask, fake_tsf_color, fake_tsf_mask = \
                self.generator.forward(bg_inputs, src_inputs, tsf_inputs, T=T)

            fake_src_imgs = fake_src_mask * bg_inpaint + (1 - fake_src_mask) * fake_src_color
            fake_tsf_imgs = fake_tsf_mask * bg_inpaint + (1 - fake_tsf_mask) * fake_tsf_color

            if self._opt.front_warp:
                fake_tsf_imgs = warp(fake_tsf_imgs, tsf_inputs[:, 0:3], tsf_fim, fake_tsf_mask)

            cycle_bg_inputs, cycle_src_inputs, cycle_tsf_inputs = set_cycle_inputs(
                fake_tsf_imgs, src_inputs, tsf_inputs, T_cycle)

            cycle_bg, cycle_src_color, cycle_src_mask, cycle_tsf_color, cycle_tsf_mask = \
                self.generator.forward(cycle_bg_inputs, cycle_src_inputs, cycle_tsf_inputs, T=T_cycle)

            cycle_src_imgs = cycle_src_mask * bg_inpaint + (1 - cycle_src_mask) * cycle_src_color
            cycle_tsf_imgs = cycle_tsf_mask * bg_inpaint + (1 - cycle_tsf_mask) * cycle_tsf_color

            if self._opt.front_warp:
                cycle_tsf_imgs = warp(cycle_tsf_imgs, src_inputs[:, 0:3], src_fim, fake_src_mask)

            return fake_src_imgs, fake_tsf_imgs, cycle_src_imgs, cycle_tsf_imgs, fake_src_mask, fake_tsf_mask

        def create_criterion():
            face_criterion = FaceLoss(pretrained_path=self._opt.face_model).cuda()
            idt_criterion = torch.nn.L1Loss()
            mask_criterion = torch.nn.BCELoss()

            return face_criterion, idt_criterion, mask_criterion

        def print_losses(*args, **kwargs):

            print('epoch = {}, step = {}'.format(kwargs['epoch'], kwargs['step']))
            for key, value in kwargs.items():
                if key == 'epoch' or key == 'step':
                    continue
                print('\t{}, {:.6f}'.format(key, value.item()))

        def update_learning_rate(optimizer, current_lr, init_lr, final_lr, nepochs_decay):
            # updated learning rate G
            lr_decay = (init_lr - final_lr) / nepochs_decay
            current_lr -= lr_decay
            for param_group in optimizer.param_groups:
                param_group['lr'] = current_lr
            print('update G learning rate: %f -> %f' % (current_lr + lr_decay, current_lr))
            return current_lr

        init_lr = 0.0002
        cur_lr = init_lr
        final_lr = 0.00001
        nodecay_epochs = 5
        nepochs_decay = 0
        optimizer = torch.optim.Adam(self.generator.parameters(), lr=init_lr, betas=(0.5, 0.999))
        face_cri, idt_cri, msk_cri = create_criterion()

        step = 0
        for epoch in range(nodecay_epochs + nepochs_decay):
            for i, sample in enumerate(data_loader):
                src_fim, tsf_fim, j2ds, T, T_cycle, T_cycle_vis, bg_inputs, src_inputs, tsf_inputs, \
                images, init_preds, pseudo_masks = set_gen_inputs(sample)

                # print(bg_inputs.shape, src_inputs.shape, tsf_inputs.shape)
                bs = tsf_inputs.shape[0]
                src_imgs = images[0:bs]
                fake_src_imgs, fake_tsf_imgs, cycle_src_imgs, cycle_tsf_imgs, fake_src_mask, fake_tsf_mask = inference(
                    bg_inputs, src_inputs, tsf_inputs, T, T_cycle, src_fim, tsf_fim)

                # cycle reconstruction loss
                cycle_loss = idt_cri(src_imgs, fake_src_imgs) + idt_cri(src_imgs, cycle_tsf_imgs)

                # structure loss
                bg_mask = src_inputs[:, -1:]
                body_mask = 1 - bg_mask
                str_src_imgs = src_imgs * body_mask
                cycle_warp_imgs = F.grid_sample(fake_tsf_imgs, T_cycle)
                back_head_mask = 1 - self.render.encode_front_fim(tsf_fim, transpose=True, front_fn=False)
                struct_loss = idt_cri(init_preds, fake_tsf_imgs) + \
                              2 * idt_cri(str_src_imgs * back_head_mask, cycle_warp_imgs * back_head_mask)

                fid_loss = face_cri(src_imgs, cycle_tsf_imgs, kps1=j2ds[:, 0], kps2=j2ds[:, 0]) + \
                           face_cri(init_preds, fake_tsf_imgs, kps1=j2ds[:, 1], kps2=j2ds[:, 1])

                # mask loss
                mask_loss = msk_cri(fake_tsf_mask, tsf_inputs[:, -1:]) + msk_cri(fake_src_mask, src_inputs[:, -1:])

                loss = 10 * cycle_loss + 10 * struct_loss + fid_loss + 5 * mask_loss
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                print_losses(epoch=epoch, step=step, total=loss, cyc=cycle_loss,
                             str=struct_loss, fid=fid_loss, msk=mask_loss)

                if step % 10 == 0:
                    self.visualize(visualizer, input_imgs=images, tsf_imgs=fake_tsf_imgs,
                                   cyc_imgs=cycle_tsf_imgs, fake_tsf_mask=fake_tsf_mask)

                step += 1

            # if epoch > nodecay_epochs:
            #     cur_lr = update_learning_rate(optimizer, cur_lr, init_lr, final_lr, nepochs_decay)

        self.generator.eval()
