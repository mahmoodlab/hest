from ultralytics import YOLO
import numpy as np
import pandas as pd
import json
import cv2
from PIL import Image
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import os
from kwimage.im_cv2 import imresize
import matplotlib.collections as mc
from typing import Dict

orientations = {
    'hourglass': 0,
    'hexFilled': 1,
    'hexOpen': 2,
    'triangle': 3
}

colors = {
    'hourglass': 'g',
    'hexFilled': 'b',
    'hexOpen': 'r',
    'triangle': 'm'
}

id_to_name = {
    0: 'hexFilled',
    1: 'hexOpen',
    2: 'hourglass',
    3: 'triangle'
}


class SpotGridTemplate:

    def __init__(self, path, name, ratio):
        f = open(path)
        template = json.load(f)
        fiducial_centers, fiducials, spots = self._load_template(template)

        self.fiducial_centers = fiducial_centers
        self.fiducials = fiducials
        self.spots = spots
        self.name = name
        self.raw_dict = template
        self.ratio = ratio

    def _load_template(self, template):
        fiducial_centers = _get_fiducials_center(template)
        oligos = template['oligo']
        spots = []
        for oligo in oligos:
            spots.append([oligo['x'], oligo['y']])
        fiducials = []
        for fid in template['fiducial']:
            fiducials.append([fid['x'], fid['y']])

        return fiducial_centers, np.array(fiducials), np.array(spots)


def _get_fiducials_center(template):
    fiducials = template['fiducial']
    dict = {}
    for fidName in orientations.keys():
        x_list = []
        y_list = []
        for fid in fiducials:
            if 'fidName' in fid and fid['fidName'].lower() == fidName.lower():
                x_list.append(fid['x'])
                y_list.append(fid['y'])
        mean_x = np.mean(x_list)
        mean_y = np.mean(y_list)
        if fidName == 'triangle':
            mean_y = mean_y - 0.001*mean_y
        dict[fidName] = [mean_x, mean_y]
    return dict


template65 = SpotGridTemplate('spot_templates/template65.json', '6.5mm', ratio=19./1477)

template11 = SpotGridTemplate('spot_templates/template11.json', '11mm', ratio=20./2622)



def _spots_to_file(path, dict):
    json_object = json.dumps(dict)
    with open(path, 'w') as f:
        f.write(json_object)



def _get_orientation(box, boxes):
    x, y, _, _ = box.xywh[0].numpy().astype(int)
    min_x = int(np.min(boxes.xywh[:,0].numpy()))
    max_x = int(np.max(boxes.xywh[:,0].numpy()))
    min_y = int(np.min(boxes.xywh[:,1].numpy()))
    max_y = int(np.max(boxes.xywh[:,1].numpy()))
    
    if abs(x - min_x) < abs(x - max_x) and abs(y - min_y) < abs(y - max_y):
        return 0
    elif abs(x - max_x) < abs(x - min_x) and abs(y - min_y) < abs(y - max_y):
        return 1
    elif abs(x - min_x) < abs(x - max_x) and abs(y - max_y) < abs(y - min_y):
        return 3
    elif abs(x - max_x) < abs(x - min_x) and abs(y - max_y) < abs(y - min_y):
        return 2
    else:
        return 4

model = YOLO('best.pt')

def _resize_to_target(img):
    TARGET_PIXEL_EDGE = 1000
    print('image size is ', img.shape)
    downscale_factor = TARGET_PIXEL_EDGE / np.max(img.shape)
    downscaled_fullres = imresize(img, downscale_factor)
    return downscaled_fullres, downscale_factor


def _alignment_plot_to_file(boxes_to_match, 
                            template, 
                            edge_len, 
                            aligned_spots, 
                            factor,
                            aligned_fiducials,
                            img,
                            save_path):
    fig, ax = plt.subplots()

    i = 0
    for box in boxes_to_match:
        fidName = id_to_name[int(box.cls)]
        src = template.fiducial_centers[fidName]
        x, y, w, h = box.xywh[0].numpy().astype(int)
        ax.scatter(x, y, c='b', s=15, facecolors='none', linewidth=0.2)
        if i < 3:
            rect = patches.Rectangle((x - w/2, y - h/2), w, h, linewidth=0.2, edgecolor=colors[fidName], facecolor='none', fill=False)
        ax.add_patch(rect)
        i += 1

    ax.imshow(img)
    s_fid = template.ratio * edge_len / 2
    s_spot = (template.ratio * edge_len) / 4

    circles = [plt.Circle(aligned_spots[i] * factor, radius=s_spot) for i in range(len(aligned_spots))]
    coll = mc.PatchCollection(circles, lw=0.2, facecolor='none')
    ax.add_collection(coll)


    circles2 = [plt.Circle(aligned_fiducials[i] * factor, radius=s_fid, color='r', facecolor='none', linewidth=0.3) for i in range(len(aligned_fiducials))]
    col2 = mc.PatchCollection(circles2, color='r', alpha=1, lw=0.25, facecolor='none')
    ax.add_collection(col2)

    plt.savefig(save_path, dpi=300)


def _match_template_type(img, boxes):
    x, y, w, h = boxes[0].xywh[0].numpy().astype(int)

    # measure the four edges along the fiducials
    edge_lengths = []
    for box1 in boxes:
        for box2 in boxes:
            fidName1 = id_to_name[int(box1.cls)]
            fidName2 = id_to_name[int(box2.cls)]
            diff = abs(orientations[fidName1] - orientations[fidName2])
            if diff == 1:
                x1, y1, _, _ = box1.xywh[0].numpy().astype(int)
                x2, y2, _, _ = box2.xywh[0].numpy().astype(int)
                edge_length = np.sqrt(((x1 - x2) ** 2) + ((y1 - y2) ** 2))
                edge_lengths.append(edge_length)

    fid_widths = []
    for box in boxes:
        x, y, w, h = box.xywh[0].numpy().astype(int)
        fid_widths.append(w)

    # estimate ratio of edge_length over fiducial width
    # this gives us a good indication of if the slide is
    # a 6.5mm or 11mm
    ratio = np.mean(edge_lengths) / np.mean(fid_widths)


    if ratio > 25:
        return template11, np.mean(edge_lengths)
    else:
        return template65, np.mean(edge_lengths)


def _spots_to_json(template, spots):
    dict = {}
    arr_spots = []
    template_dict = template.raw_dict
    for i in range(len(template_dict['oligo'])):
        oligo = template_dict['oligo'][i]
        arr_spots.append({
            'tissue': True,
            'row': oligo['row'],
            'col': oligo['col'],
            'imageX': spots[i][0],
            'imageY': spots[i][1]
        })
    dict['oligo'] = arr_spots


def autoalign_with_fiducials(fullres_img, save_dir=None, name='') -> Dict:

    img, factor = _resize_to_target(fullres_img)
    result = model(img)[0]

    if len(result.boxes) == 0:
        raise Exception("couldn't find any fiducials")


    # discard fiducials prediction that didn't agree
    # with the majority vote
    boxes_to_match = []
    for i in range(len(result.boxes)):
        #if diffs[i] == highest:
        boxes_to_match.append(result.boxes[i])
    
    if len(boxes_to_match) < 3:
        raise Exception('Auto-alignment failed to detect at least 3 fiducials')
    
    template, edge_len = _match_template_type(img, boxes_to_match)

    src_pts = []
    dst_pts = []
    for box in boxes_to_match:
        fidName = id_to_name[int(box.cls)]
        src = template.fiducial_centers[fidName]
        x, y, w, h = box.xywh[0].numpy().astype(int)
        dst = [x, y]

        src_pts.append(src)
        dst_pts.append(dst)

    n = len(boxes_to_match)
    if n >= 3:
        found = False
        for i in range(n):
            if found:
                break
            for j in range(n):
                if found:
                    break
                for k in range(n):
                    if i == j or i == k or k == j:
                        continue
                    
                    my_src_pts = np.array(src_pts)[[i, j, k]].astype(np.float32)
                    my_dst_pts = np.array(dst_pts)[[i, j, k]].astype(np.float32)

                    warp_mat = cv2.getAffineTransform(my_src_pts, my_dst_pts) * (1 / factor)
                    
                    fiducials = np.column_stack((template.fiducials, np.ones((template.fiducials.shape[0],))))
                    aligned_fiducials = warp_mat @ fiducials.T
                    aligned_fiducials = aligned_fiducials.T
                    
                    min_y = np.min(aligned_fiducials[:, 0])
                    max_y = np.max(aligned_fiducials[:, 0])
                    min_x = np.min(aligned_fiducials[:, 1])
                    max_x = np.max(aligned_fiducials[:, 1])
                    max_width_height = int(np.max(fullres_img.shape)) # we need this lie as the image might be rotated
                    if min_y > 0 and min_x > 0 and max_y < max_width_height and max_x < max_width_height:
                        found = True
                        break
                    
        if not found:
            raise Exception("couldn't find alignment, make sure that all the fiducials are visible")
    

    spots = np.column_stack((template.spots, np.ones((template.spots.shape[0],))))
    aligned_spots = warp_mat @ spots.T
    aligned_spots = aligned_spots.T
                    
    
    dict =  _spots_to_json(template, spots)
    
    if save_dir is not None:
        save_path = os.path.join(save_dir, name + 'autoalignment.png')

        _alignment_plot_to_file(boxes_to_match, 
                                template, 
                                edge_len, 
                                aligned_spots, 
                                factor,
                                aligned_fiducials,
                                img,
                                save_path)


        _spots_to_file(os.path.join(save_dir, name + 'autoalignment.json'), dict)
        
        
    return dict
