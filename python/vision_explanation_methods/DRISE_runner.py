import torch
import torchvision
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
import torchvision.models.detection as detection
from torchvision import transforms as T
from captum.attr import visualization as viz

import numpy
import matplotlib.pyplot as plt
from matplotlib import patches
import PIL.Image as Image


import os
import argparse

from typing import Tuple, Optional, List

from .explanations import drise
from .explanations import common as od_common



class PytorchFasterRCNNWrapper(od_common.GeneralObjectDetectionModelWrapper):
    """Wraps a PytorchFasterRCNN model with a predict API function for object detection.

    To be compatible with the drise explainability method, all models must be wrapped to have
    the same output and input class.
    This wrapper is customized for the FasterRCNN model from Pytorch, and can
    also be used with the RetinaNet or any other models with the same output class.

    :param model: Object detection model
    :type model: PytorchFasterRCNN model
    :param number_of_classes: Number of classes the model is predicting
    :type number_of_classes: int
    """
    
    def __init__(self, model, number_of_classes: int):
        self._model = model
        self._number_of_classes = number_of_classes

    def predict(self, x: torch.Tensor) -> List[od_common.DetectionRecord]:
        """Creates a list of detection records from the image predictions.

        :param x: Tensor of the image
        :type x: torch.Tensor
        :return: Baseline detections to get saliency maps for
        :rtype: List of Detection Records
        """
        raw_detections = self._model(x)

        def apply_nms(orig_prediction: dict, iou_thresh: float=0.5):
            """Performs non maximum suppression on the predictions according to their intersection-over-union.

            :param orig_prediction: Original model prediction 
            :type orig_prediction: dict
            :param iou_thresh: iou_threshold for nms
            :type iou_thresh: float
            :return: Model prediction after nms is applied
            :rtype: dict
            """
            keep = torchvision.ops.nms(orig_prediction['boxes'], orig_prediction['scores'], iou_thresh)

            nms_prediction = orig_prediction
            nms_prediction['boxes'] = nms_prediction['boxes'][keep]
            nms_prediction['scores'] = nms_prediction['scores'][keep]
            nms_prediction['labels'] = nms_prediction['labels'][keep]
            return nms_prediction
        
        def filter_score(orig_prediction: dict, score_thresh: float=0.5):
            """Filters out model predictions with confidence scores below score_thresh

            :param orig_prediction: Original model prediction 
            :type orig_prediction: dict
            :param score_thresh: Score threshold to filter by
            :type score_thresh: float
            :return: Model predictions filtered out by score_thresh 
            :rtype: dict
            """
            keep = orig_prediction['scores'] > score_thresh

            filter_prediction = orig_prediction
            filter_prediction['boxes'] = filter_prediction['boxes'][keep]
            filter_prediction['scores'] = filter_prediction['scores'][keep]
            filter_prediction['labels'] = filter_prediction['labels'][keep]
            return filter_prediction
        
        detections = [] 
        for raw_detection in raw_detections:
            raw_detection = apply_nms(raw_detection,0.005)
            
            # Note that FasterRCNN doesn't return a socre for each class, only the predicted class
            # DRISE requires a score for each class. We approximate the score for each class
            # by dividing the (1.0 - class score) evenly among the other classes.
            
            raw_detection = filter_score(raw_detection, 0.2)
            expanded_class_scores = od_common.expand_class_scores(raw_detection['scores'],
                                                                  raw_detection['labels'],
                                                                  self._number_of_classes)
            detections.append(
                od_common.DetectionRecord(
                    bounding_boxes=raw_detection['boxes'],
                    class_scores=expanded_class_scores,
                    objectness_scores=torch.tensor([1.0]*raw_detection['boxes'].shape[0]),
                    
                )
            )
        
        return detections


def plot_img_bbox(ax, box: numpy.ndarray, label: str, color: str):
    """Plots predicted bounding box and label on top of the d-rise generated saliency map.

    :param ax: Axis on which the d-rise saliency map was plotted 
    :type ax: Matplotlib AxesSubplot
    :param box: Bounding box the model predicted
    :type box: numpy.ndarray
    :param label: Label the model predicted 
    :type label: str
    :param color: Color of the bounding box based on predicted label
    :type color: single letter color string
    :return: Axis with the predicted bounding box and label plotted on top of d-rise saliency map
    :rtype: 
    """
    x, y, width, height = box[0], box[1], box[2]-box[0], box[3]-box[1]
    rect = patches.Rectangle((x, y),
                            width, height,
                            linewidth = 2,
                            edgecolor = color,
                            facecolor = 'none',
                            label=label)
    ax.add_patch(rect)
    frame = ax.get_position()
    ax.set_position([frame.x0, frame.y0, frame.width * 0.8, frame.height])

    ax.legend(loc='center left', bbox_to_anchor=(1, 0.5))
    return ax


def get_instance_segmentation_model(num_classes: int):
    """Load in pre-trained Faster R-CNN model with resnet50 backbone.

    :param num_classes: Number of classes model predicted
    :type num_classes: int
    :return: Faster R-CNN PyTorch model 
    :rtype: PyTorch model
    """
    model = torchvision.models.detection.fasterrcnn_resnet50_fpn(pretrained=True)
    in_features = model.roi_heads.box_predictor.cls_score.in_features
    # replace the pre-trained head with a new one
    model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)
    
    return model


def get_drise_saliency_map(
    imagelocation: str,
    modellocation: Optional[str],
    numclasses: int,
    savename: str,
    nummasks: int=25,
    maskres: Tuple[int, int]=(4,4),
    devicechoice: Optional[str]=None
    ):
    """Run D-RISE on image and visualize the saliency maps

    :param imagelocation: Path for the image location.
    :type imagelocation: str
    :param modellocation: Path for the model location. If None, pre-trained Faster R-CNN model will be used.
    :type modellocation: Optional str
    :param numclasses: Number of classes model predicted
    :type numclasses: int
    :param savename: Path for the saved output figure. 
    :type savename: str
    :param nummasks: Number of masks to use for saliency
    :type nummasks: int
    :param maskres: Resolution of mask before scale up
    :type maskres: Tuple of ints
    :param devicechoice: Device to use to run the function
    :type devicechoice: str
    :return: Tuple of Matplotlib figure and string path to where the output figure is saved 
    :rtype: Tuple of Matplotlib figure, str
    """
    
    if not devicechoice:
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    else: device = devicechoice

    if not modellocation:
        # If user did not specify a model location, we simply load in the pytorch pre-trained model.
        print("using pretrained model")
        model = detection.fasterrcnn_resnet50_fpn(pretrained=True,map_location=device)
        numclasses = 91

    else:
        print("loading user model")
        model = get_instance_segmentation_model(numclasses)
        model.load_state_dict(torch.load(modellocation,map_location=device))

    test_image = Image.open(imagelocation).convert('RGB')

    model = model.to(device)
    model.eval()

    print(PytorchFasterRCNNWrapper)
    explainable_wrapper = PytorchFasterRCNNWrapper(model, numclasses)

    detections = explainable_wrapper.predict(T.ToTensor()(test_image).unsqueeze(0).repeat(2, 1, 1, 1).to(device))
    saliency_scores = drise.DRISE_saliency(
        model=explainable_wrapper,
        image_tensor=T.ToTensor()(test_image).repeat(2, 1, 1, 1).to(device), # Repeated the tensor to test batching.
        target_detections=detections,
        number_of_masks=nummasks, # This is how many masks to run - more is slower but gives higher quality mask.
        device=device,
        mask_res=maskres, # This is the resolution of the random masks. High resolutions will give finer masks, but more need to be run.
        verbose=True # Turns progress bar on/off.
    ) 

    img_index = 0
    saliency_scores = [saliency_scores[img_index][i] for i in range(len(saliency_scores[img_index])) 
    if torch.isnan(saliency_scores[img_index][i]['detection']).any() == False] #exclude scores containing NaN
    num_detections = len(saliency_scores)

    if num_detections == 0: #if no objects have been detected... 
        fail = Image.open(os.path.join("images","notfound.png")) 
        fail = fail.save(savename)
        return None,None

    fig, axis = plt.subplots(1, num_detections,figsize= (num_detections*10,10))

    for i in range(num_detections):
        viz.visualize_image_attr(
            numpy.transpose(saliency_scores[i]['detection'].cpu().detach().numpy(), (1, 2, 0)), #drise runner usually has access to an images folder
            # The [0][0] means first image, first detection.
            numpy.transpose(T.ToTensor()(test_image).numpy(), (1, 2, 0)),
            method="blended_heat_map",
            sign="positive",
            show_colorbar=True,
            cmap=plt.cm.inferno,
            title="Detection " + str(i),
            plt_fig_axis = (fig, axis[i]),
            use_pyplot = False
        )

        box = detections[img_index].bounding_boxes[i].detach().numpy() 
        label = int(torch.argmax(detections[img_index].class_scores[i]))  
        if num_detections>1: #if there is more than one element to display, hence multiple subplots
            #applicable only to recycling dataset.
            axis[i] = plot_img_bbox(axis[i],box,str(label),'r')
        elif type(axis)!=list: 
            axis = plot_img_bbox(axis,box,str(label),'r')
        else: #unclear why, but sometimes even with just one element axis needs to be indexed
            axis[i] = plot_img_bbox(axis[i],box,str(label),'r')
        fig.savefig(savename)
    return fig,savename



if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--imagelocation", default='images/cartons.jpg',\
        help = "image subdirectory. Default: images/cartons.jpg", type=str)
    parser.add_argument("--modellocation", default=None ,help = "fine-tuned model subdirectory. Default: pre-trained FastRCNN from Pytorch")
    parser.add_argument("--numclasses", default=91 ,help = "number of classes. Default: 91",type=int) #interestingly, not enforcing int made it a float ;v;
    parser.add_argument("--savename", default='res/outputmaps.jpg' ,help = "exported Filename. Default: res/outputmaps.jpg", type=str)
    
    parser.add_argument("--nummasks", default=25 ,help = "number of masks. Default: 25", type=int)
    parser.add_argument("--maskres", default=(4,4) ,help = "mask resolution. Default: (4,4) ", type=tuple)
    parser.add_argument("--device",default=None, help="enforce certain device. Default: cuda:0 if available, cpu if not.", type=str)

    args = parser.parse_args()

    res = get_drise_saliency_map(args.imagelocation, args.modellocation, args.numclasses, args.savename, args.nummasks, args.maskres, args.device)