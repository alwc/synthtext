import copy
import h5py
from PIL import Image
import numpy as np
#import mayavi.mlab as mym
import matplotlib.pyplot as plt
import os.path as osp
import scipy.ndimage as sim
import scipy.spatial.distance as ssd
import traceback, itertools

import cv2

import synthtext.synth as synth
import synthtext.text_renderer as text_renderer
from synthtext.colorizer import Colorizer
from synthtext.common import TimeoutException, time_limit
from synthtext.config import load_cfg

from .text_regions import TEXT_REGIONS
from .utils import rescale_frontoparallel, get_text_placement_mask
from .utils import get_bounding_rect, get_crops, filter_valid
from .viz import viz_textbb, viz_images


class Renderer(object):
    def __init__(self):
        load_cfg(self)
        self.text_render = text_renderer.TextRenderer()
        self.colorizer = Colorizer()

    def filter_regions(self, regions, filt):
        """
        filt : boolean list of regions to keep.
        """
        idx = np.arange(len(filt))[filt]
        for k in regions.keys():
            regions[k] = [regions[k][i] for i in idx]
        return regions

    def filter_for_placement(self, xyz, seg, regions):
        filt = np.zeros(len(regions['label'])).astype('bool')
        masks, Hs, Hinvs = [], [], []
        for idx, l in enumerate(regions['label']):
            res = get_text_placement_mask(xyz,
                                          seg == l,
                                          regions['coeff'][idx],
                                          pad=2)
            if res is not None:
                mask, H, Hinv = res
                masks.append(mask)
                Hs.append(H)
                Hinvs.append(Hinv)
                filt[idx] = True
        regions = self.filter_regions(regions, filt)
        regions['place_mask'] = masks
        regions['homography'] = Hs
        regions['homography_inv'] = Hinvs

        return regions

    def warpHomography(self, src_mat, H, dst_size):
        dst_mat = cv2.warpPerspective(src_mat,
                                      H,
                                      dst_size,
                                      flags=cv2.WARP_INVERSE_MAP
                                      | cv2.INTER_LINEAR)
        return dst_mat

    def homographyBB(self, bbs, H, offset=None):
        """
        Apply homography transform to bounding-boxes.
        BBS: 2 x 4 x n matrix  (2 coordinates, 4 points, n bbs).
        Returns the transformed 2x4xn bb-array.

        offset : a 2-tuple (dx,dy), added to points before transfomation.
        """
        eps = 1e-16
        # check the shape of the BB array:
        t, f, n = bbs.shape
        assert (t == 2) and (f == 4)

        # append 1 for homogenous coordinates:
        bbs_h = np.reshape(np.r_[bbs, np.ones((1, 4, n))], (3, 4 * n),
                           order='F')
        if offset != None:
            bbs_h[:2, :] += np.array(offset)[:, None]

        # perpective:
        bbs_h = H.dot(bbs_h)
        bbs_h /= (bbs_h[2, :] + eps)

        bbs_h = np.reshape(bbs_h, (3, 4, n), order='F')
        return bbs_h[:2, :, :]

    def bb_filter(self, bb0, bb, text):
        """
        Ensure that bounding-boxes are not too distorted
        after perspective distortion.

        bb0 : 2x4xn martrix of BB coordinates before perspective
        bb  : 2x4xn matrix of BB after perspective
        text: string of text -- for excluding symbols/punctuations.
        """
        h0 = np.linalg.norm(bb0[:, 3, :] - bb0[:, 0, :], axis=0)
        w0 = np.linalg.norm(bb0[:, 1, :] - bb0[:, 0, :], axis=0)
        hw0 = np.c_[h0, w0]

        h = np.linalg.norm(bb[:, 3, :] - bb[:, 0, :], axis=0)
        w = np.linalg.norm(bb[:, 1, :] - bb[:, 0, :], axis=0)
        hw = np.c_[h, w]

        # remove newlines and spaces:
        text = ''.join(text.split())
        assert len(text) == bb.shape[-1]

        alnum = np.array([ch.isalnum() for ch in text])
        hw0 = hw0[alnum, :]
        hw = hw[alnum, :]

        min_h0, min_h = np.min(hw0[:, 0]), np.min(hw[:, 0])
        asp0, asp = hw0[:, 0] / hw0[:, 1], hw[:, 0] / hw[:, 1]
        asp0, asp = np.median(asp0), np.median(asp)

        asp_ratio = asp / asp0
        is_good = (min_h > self.min_char_height
                   and asp_ratio > self.min_asp_ratio
                   and asp_ratio < 1.0 / self.min_asp_ratio)
        return is_good

    def get_min_h(selg, bb, text):
        # find min-height:
        h = np.linalg.norm(bb[:, 3, :] - bb[:, 0, :], axis=0)
        # remove newlines and spaces:
        text = ''.join(text.split())
        assert len(text) == bb.shape[-1]

        alnum = np.array([ch.isalnum() for ch in text])
        h = h[alnum]
        return np.min(h)

    def feather(self, text_mask, min_h):
        # determine the gaussian-blur std:
        if min_h <= 15:
            bsz = 0.25
            ksz = 1
        elif 15 < min_h < 30:
            bsz = max(0.30, 0.5 + 0.1 * np.random.randn())
            ksz = 3
        else:
            bsz = max(0.5, 1.5 + 0.5 * np.random.randn())
            ksz = 5
        return cv2.GaussianBlur(text_mask, (ksz, ksz), bsz)

    def place_text(self, rgb, collision_mask, H, Hinv):

        render_res = self.text_render.render_text(collision_mask)
        if render_res is None:  # rendering not successful
            return  #None
        else:
            text_mask, loc, bb, text, curve_flag = render_res
            #if not curve_flag:
            #    return

        # update the collision mask with text:
        collision_mask += (255 * (text_mask > 0)).astype('uint8')

        # warp the object mask back onto the image:
        text_mask_orig = text_mask.copy()

        ######### start #########
        #viz_textbb(1, text_mask_orig, [wordBB], alpha=1.0)
        #fignum = 1
        #plt.figure(fignum)
        #plt.imshow(text_mask_orig)
        #plt.show(block=False)
        #import pdb
        #pdb.set_trace()
        #input('continue?')
        #plt.close(fignum)
        ######### end #########

        bb_orig = bb.copy()
        text_mask = self.warpHomography(text_mask, H, rgb.shape[:2][::-1])
        bb = self.homographyBB(bb, Hinv)

        ### start
        #wordBB = self.char2wordBB(bb.copy(), text)
        #viz_textbb(1, text_mask, [wordBB], alpha=1.0)

        #fy = rgb.shape[0] / text_mask.shape[0] # height
        #fx = rgb.shape[1] / text_mask.shape[1] # width
        #text_mask = cv2.resize(text_mask, rgb.shape[:2][::-1])
        #bb = np.array([[fx], [fy]])[:, :, None] * bb

        #wordBB = self.char2wordBB(bb.copy(), text)
        #viz_textbb(2, text_mask, [wordBB], alpha=1.0)
        #import pdb
        #pdb.set_trace()
        ### end

        if not self.bb_filter(bb_orig, bb, text):
            #warn('bad charBB statistics')
            return  #None

        # get the minimum height of the character-BB:
        min_h = self.get_min_h(bb, text)

        #feathering:
        text_mask = self.feather(text_mask, min_h)

        im_final = self.colorizer.colorize(rgb, [text_mask], np.array([min_h]))

        return im_final, text, bb, collision_mask, curve_flag

    def get_num_text_regions(self, nregions):
        #return nregions
        nmax = min(self.max_text_regions, nregions)
        if np.random.rand() < 0.10:
            rnd = np.random.rand()
        else:
            rnd = np.random.beta(5.0, 1.0)
        return int(np.ceil(nmax * rnd))

    def char2wordBB(self, charBB, text):
        """
        Converts character bounding-boxes to word-level
        bounding-boxes.

        charBB : 2x4xn matrix of BB coordinates
        text   : the text string

        output : 2x4xm matrix of BB coordinates,
                 where, m == number of words.
        """
        wrds = text.split()
        bb_idx = np.r_[0, np.cumsum([len(w) for w in wrds])]
        wordBB = np.zeros((2, 4, len(wrds)), 'float32')

        for i in range(len(wrds)):
            cc = charBB[:, :, bb_idx[i]:bb_idx[i + 1]]

            # fit a rotated-rectangle:
            # change shape from 2x4xn_i -> (4*n_i)x2
            cc = np.squeeze(np.concatenate(np.dsplit(cc, cc.shape[-1]),
                                           axis=1)).T.astype('float32')
            rect = cv2.minAreaRect(cc.copy())
            box = np.array(cv2.boxPoints(rect))

            # find the permutation of box-coordinates which
            # are 'aligned' appropriately with the character-bb.
            # (exhaustive search over all possible assignments):
            cc_tblr = np.c_[cc[0, :], cc[-3, :], cc[-2, :], cc[3, :]].T
            perm4 = np.array(list(itertools.permutations(np.arange(4))))
            dists = []
            for pidx in range(perm4.shape[0]):
                d = np.sum(
                    np.linalg.norm(box[perm4[pidx], :] - cc_tblr, axis=1))
                dists.append(d)
            wordBB[:, :, i] = box[perm4[np.argmin(dists)], :].T

        return wordBB

    # main
    def render(self, rgb, depth, seg, area, label, ninstance=1, viz=False):
        """
        rgb   : HxWx3 image rgb values (uint8)
        depth : HxW depth values (float)
        seg   : HxW segmentation region masks
        area  : number of pixels in each region
        label : region labels == unique(seg) / {0}
               i.e., indices of pixels in SEG which
               constitute a region mask
        ninstance : no of times image should be
                    used to place text.

        @return:
            res : a list of dictionaries, one for each of 
                  the image instances.
                  Each dictionary has the following structure:
                      'img' : rgb-image with text on it.
                      'bb'  : 2x4xn matrix of bounding-boxes
                              for each character in the image.
                      'txt' : a list of strings.

                  The correspondence b/w bb and txt is that
                  i-th non-space white-character in txt is at bb[:,:,i].
            
            If there's an error in pre-text placement, for e.g. if there's 
            no suitable region for text placement, an empty list is returned.
        """
        #try:
        # depth -> xyz
        # TODO: debug
        #np.random.seed(0)
        #depth = 100 + 0.00 * np.random.rand(*depth.shape)
        xyz = synth.DepthCamera.depth2xyz(depth)
        #import pdb
        #pdb.set_trace()
        #xyz = 100 + 0.0 * np.random.rand(*xyz.shape)

        # find text-regions:
        regions = TEXT_REGIONS.get_regions(xyz, seg, area, label)

        # find the placement mask and homographies:
        regions = self.filter_for_placement(xyz, seg, regions)

        # finally place some text:
        nregions = len(regions['place_mask'])
        if nregions < 1:  # no good region to place text on
            return []
        #except:
        # failure in pre-text placement
        #import traceback
        #    traceback.print_exc()
        #    return []

        res = []
        for i in range(ninstance):
            print('-----Instance %d-----' % i)
            place_masks = copy.deepcopy(regions['place_mask'])

            idict = {'img': [], 'charBB': None, 'wordBB': None, 'txt': None}

            m = self.get_num_text_regions(
                nregions
            )  #np.arange(nregions)#min(nregions, 5*ninstance*self.max_text_regions))
            reg_idx = np.arange(min(2 * m, nregions))
            np.random.shuffle(reg_idx)
            reg_idx = reg_idx[:m]

            img = rgb.copy()
            itext = []
            ibb = []

            # process regions:
            num_txt_regions = len(reg_idx)
            reg_range = np.arange(
                self.num_repeat * num_txt_regions) % num_txt_regions
            placed = False
            for idx in reg_range:
                ireg = reg_idx[idx]
                try:
                    if self.max_time is None:
                        txt_render_res = self.place_text(
                            img, place_masks[ireg],
                            regions['homography'][ireg],
                            regions['homography_inv'][ireg])
                    else:
                        with time_limit(self.max_time):
                            txt_render_res = self.place_text(
                                img, place_masks[ireg],
                                regions['homography'][ireg],
                                regions['homography_inv'][ireg])
                except TimeoutException as e:
                    print(e)
                    continue
                #except:
                #    traceback.print_exc()
                #    # some error in placing text on the region
                #    continue

                if txt_render_res is not None:
                    placed = True
                    img, text, bb, collision_mask, curve_flag = txt_render_res
                    # update the region collision mask:
                    place_masks[ireg] = collision_mask
                    # store the result:
                    itext.append(text)
                    ibb.append(bb)

                    #print('-----<text-----')
                    #if curve_flag:
                    #    print('####curve####')
                    #else:
                    #    print('####not curve####')
                    #print(text)
                    #print('-----text>-----')

                    # at least 1 word was placed in this instance:
                    idict['img'] = img
                    idict['txt'] = itext
                    idict['charBB'] = np.concatenate(ibb, axis=2)
                    idict['wordBB'] = self.char2wordBB(idict['charBB'].copy(),
                                                       ' '.join(itext))
                    res.append(idict.copy())
            if placed:
                if viz:

                    #min_area_rect = idict['wordBB']
                    #texts = []
                    #for line in idict['txt']:
                    #    segs = line.split()
                    #    texts.extend(segs)
                    #four_points, extreme_points = get_bounding_rect(min_area_rect)
                    #crops, valid_flags = get_crops(img, extreme_points)
                    #print(len(crops), len(texts))
                    #valid_crops = filter_valid(crops, valid_flags)
                    #valid_texts = filter_valid(texts, valid_flags)
                    #viz_images(1, valid_crops, valid_texts)
                    #viz_textbb(1, img, [idict['wordBB']], alpha=1.0)
                    #viz_textbb(1, img, [bbox], alpha=1.0)
                    #viz_masks(2, img, seg, depth, regions['label'])
                    # viz_regions(rgb.copy(),xyz,seg,regions['coeff'],regions['label'])
                    if i < ninstance - 1:
                        input('continue?')
        return res
