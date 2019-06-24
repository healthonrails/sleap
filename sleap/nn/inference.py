import sys
import argparse
import multiprocessing
import os
import json
import logging
logger = logging.getLogger(__name__)

import numpy as np
import pandas as pd
import cv2
import keras
import attr

from time import time
from typing import Dict, List, Union, Optional

from scipy.ndimage import maximum_filter, gaussian_filter
from keras.utils import multi_gpu_model

from sleap.instance import LabeledFrame, PredictedPoint, PredictedInstance
from sleap.io.dataset import Labels
from sleap.io.video import Video
from sleap.skeleton import Skeleton
from sleap.util import usable_cpu_count
from sleap.nn.model import ModelOutputType
from sleap.nn.training import TrainingJob
from sleap.nn.tracking import FlowShiftTracker, Track


def get_available_gpus():
    """
    Get the list of available GPUs

    Returns:
        List of available GPU device names
    """

    from tensorflow.python.client import device_lib
    local_device_protos = device_lib.list_local_devices()
    return [x.name for x in local_device_protos if x.device_type == 'GPU']

def get_inference_model(confmap_model_path: str, paf_model_path: str, new_input_size: None) -> keras.Model:
    """ Loads and merges confmap and PAF models into one. """

    # Load
    confmap_model = keras.models.load_model(confmap_model_path)
    paf_model = keras.models.load_model(paf_model_path)

    logger.info(f'confmap model trained on shape {confmap_model.input_shape}')
    logger.info(f'paf model trained on shape {paf_model.input_shape}')

    # Single input
    if new_input_size is None:
        new_input = confmap_model.input
    else:
        # create new input layout with given size (h, w, channels)
        # this allows us to use model trained on cropped images
        new_input = keras.layers.Input(new_input_size)
        logger.info(f'adjusting model input shape to {new_input_size}')

    # Rename to prevent layer naming conflict
    confmap_model.name = "confmap_" + confmap_model.name
    paf_model.name = "paf_" + paf_model.name
    for i in range(len(confmap_model.layers)):
        confmap_model.layers[i].name = "confmap_" + confmap_model.layers[i].name
    for i in range(len(paf_model.layers)):
        paf_model.layers[i].name = "paf_" + paf_model.layers[i].name

    # Get rid of first layer
    confmap_model.layers.pop(0)
    paf_model.layers.pop(0)

    # Combine models with tuple output
    model = keras.Model(new_input, [confmap_model(new_input), paf_model(new_input)])

    gpu_list = get_available_gpus()

    if len(gpu_list) == 0:
        logger.warn('No GPU devices, this is going to be really slow, something is wrong, dont do this!!!')
    else:
        logger.info(f'Detected {len(gpu_list)} GPU(s) for inference')

    if len(gpu_list) > 1:
        model = multi_gpu_model(model, gpus=len(gpu_list))

    return model


def impeaksnms(I, min_thresh=0.3, sigma=3, return_val=True):
    """ Find peaks via non-maximum suppresion. """

    # Threshold
    if min_thresh is not None:
        I[I < min_thresh] = 0

    # Blur
    if sigma is not None:
        I = gaussian_filter(I, sigma=sigma, mode="constant", cval=0, truncate=8)

    # Maximum filter
    kernel = np.array([[1,1,1],
                       [1,0,1],
                       [1,1,1]])
    m = maximum_filter(I, footprint=kernel, mode="constant", cval=0)

    # Convert to points
    r, c = np.nonzero(I > m)
    pts = np.stack((c, r), axis=1)

    # Return
    if return_val:
        vals = np.array([I[pt[1],pt[0]] for pt in pts])
        return pts, vals
    else:
        return pts


def impeaksnms_cv(I, min_thresh=0.3, sigma=3, return_val=True):
    """ Find peaks via non-maximum suppresion using OpenCV. """

    # Threshold
    if min_thresh is not None:
        I[I < min_thresh] = 0

    # Blur
    if sigma is not None:
        I = cv2.GaussianBlur(I, (9,9), sigma)

    # Maximum filter
    kernel = np.array([[1,1,1],
                       [1,0,1],
                       [1,1,1]]).astype("uint8")
    m = cv2.dilate(I, kernel)

    # Convert to points
    r, c = np.nonzero(I > m)
    pts = np.stack((c, r), axis=1)

    # Return
    if return_val:
        vals = np.array([I[pt[1],pt[0]] for pt in pts])
        return pts.astype("float32"), vals
    else:
        return pts.astype("float32")


def find_all_peaks(confmaps, min_thresh=0.3, sigma=3):
    """ Finds peaks for all frames/channels in a stack of confidence maps """
    peaks = []
    peak_vals = []
    for confmap in confmaps:
        peaks_i = []
        peak_vals_i = []
        for i in range(confmap.shape[-1]):
            # peak, val = impeaksnms(confmap[...,i], min_thresh=min_thresh, sigma=sigma, return_val=True)
            peak, val = impeaksnms_cv(confmap[...,i], min_thresh=min_thresh, sigma=sigma, return_val=True)
            peaks_i.append(peak)
            peak_vals_i.append(val)
        peaks.append(peaks_i)
        peak_vals.append(peak_vals_i)

    return peaks, peak_vals


def improfile(I, p0, p1, max_points=None):
    """ Returns values of the image I evaluated along the line formed by points p0 and p1.

    Parameters
    ----------
    I : 2d array
        Image to get values from
    p0, p1 : 1d array with 2 elements
        Start and end coordinates of the line

    Returns
    -------
    vals : 1d array
        Vector with the images values along the line formed by p0 and p1
    """
    # Make sure image is 2d
    I = np.squeeze(I)

    # Find number of points to extract
    n = np.sqrt((p0[0] - p1[0])**2 + (p0[1] - p1[1])**2)
    n = max(n, 1)
    if max_points is not None:
        n = min(n, max_points)
    n = int(n)

    # Compute coordinates
    x = np.round(np.linspace(p0[0], p1[0], n)).astype("int32")
    y = np.round(np.linspace(p0[1], p1[1], n)).astype("int32")

    # Extract values and concatenate into vector
    vals = np.stack([I[yi,xi] for xi, yi in zip(x,y)])
    return vals

def match_peaks_frame(peaks_t, peak_vals_t, pafs_t, skeleton, scale,
                      min_score_to_node_ratio=0.4,
                      min_score_midpts=0.05,
                      min_score_integral=0.8,
                      add_last_edge=False):
    """
    Matches single frame
    """

    # Effectively the original implementation:
    # https://github.com/michalfaber/keras_Realtime_Multi-Person_Pose_Estimation/blob/master/demo_video.py#L107

    # Dumb
    peak_ids = []
    idx = 0
    for i in range(len(peaks_t)):
        idx_i = []
        for j in range(len(peaks_t[i])):
            idx_i.append(idx)
            idx += 1
        peak_ids.append(idx_i)

    # Score each edge
    special_k = []
    connection_all = []
    for k, edge in enumerate(skeleton.edge_names):
        src_node_idx = skeleton.node_to_index(edge[0])
        dst_node_idx = skeleton.node_to_index(edge[1])
        paf_x = pafs_t[...,2*k]
        paf_y = pafs_t[...,2*k+1]
        peaks_src = peaks_t[src_node_idx]
        peaks_dst = peaks_t[dst_node_idx]
        peak_vals_src = peak_vals_t[src_node_idx]
        peak_vals_dst = peak_vals_t[dst_node_idx]

        if len(peaks_src) == 0 or len(peaks_dst) == 0:
            special_k.append(k)
            connection_all.append([])
        else:
            connection_candidates = []
            for i, peak_src in enumerate(peaks_src):
                for j, peak_dst in enumerate(peaks_dst):
                    # Vector between peaks
                    vec = peak_dst - peak_src

                    # Euclidean distance between points
                    norm = np.sqrt(np.sum(vec ** 2))

                    # Failure if points overlap
                    if norm == 0:
                        continue

                    # Convert to unit vector
                    vec = vec / norm

                    # Get PAF values along edge
                    vec_x = improfile(paf_x, peak_src, peak_dst)
                    vec_y = improfile(paf_y, peak_src, peak_dst)

                    # Compute score
                    score_midpts = vec_x * vec[0] + vec_y * vec[1]
                    score_with_dist_prior = np.mean(score_midpts) + min(0.5 * paf_x.shape[0] / norm - 1, 0)
                    score_integral = np.mean(score_midpts > min_score_midpts)
                    if score_with_dist_prior > 0 and score_integral > min_score_integral:
                        connection_candidates.append([i, j, score_with_dist_prior])

            # Sort candidates for current edge by descending score
            connection_candidates = sorted(connection_candidates, key=lambda x: x[2], reverse=True)

            # Add to list of candidates for next step
            connection = np.zeros((0,5)) # src_id, dst_id, paf_score, i, j
            for candidate in connection_candidates:
                i, j, score = candidate
                # Add to connections if node is not already included
                if (i not in connection[:, 3]) and (j not in connection[:, 4]):
                    id_i = peak_ids[src_node_idx][i]
                    id_j = peak_ids[dst_node_idx][j]
                    connection = np.vstack([connection, [id_i, id_j, score, i, j]])

                    # Stop when reached the max number of matches possible
                    if len(connection) >= min(len(peaks_src), len(peaks_dst)):
                        break
            connection_all.append(connection)

    # Greedy matching of each edge candidate set
    subset = -1 * np.ones((0, len(skeleton.nodes)+2)) # ids, overall score, number of parts
    candidate = np.array([y for x in peaks_t for y in x]) # flattened set of all points
    candidate = candidate / scale # rescale points to original image
    candidate_scores = np.array([y for x in peak_vals_t for y in x]) # flattened set of all peak scores
    for k, edge in enumerate(skeleton.edge_names):
        # No matches for this edge
        if k in special_k:
            continue

        # Get IDs for current connection
        partAs = connection_all[k][:,0]
        partBs = connection_all[k][:,1]

        # Get edge
        indexA, indexB = (skeleton.node_to_index(edge[0]), skeleton.node_to_index(edge[1]))

        # Loop through all candidates for current edge
        for i in range(len(connection_all[k])):
            found = 0
            subset_idx = [-1, -1]

            # Search for current candidates in matched subset
            for j in range(len(subset)):
                if subset[j][indexA] == partAs[i] or subset[j][indexB] == partBs[i]:
                    subset_idx[found] = j
                    found += 1

            # One of the two candidate points found in matched subset
            if found == 1:
                j = subset_idx[0]
                if subset[j][indexB] != partBs[i]: # did we already assign this part?
                    subset[j][indexB] = partBs[i] # assign part
                    subset[j][-1] += 1 # increment instance part counter
                    subset[j][-2] += candidate_scores[int(partBs[i])] + connection_all[k][i][2] # add peak + edge score

            # Both candidate points found in matched subset
            elif found == 2:
                j1, j2 = subset_idx # get indices in matched subset
                membership = ((subset[j1] >= 0).astype(int) + (subset[j2] >= 0).astype(int))[:-2] # count number of instances per body parts
                # All body parts are disjoint, merge them
                if np.all(membership < 2):
                    subset[j1][:-2] += (subset[j2][:-2] + 1)
                    subset[j1][-2:] += subset[j2][-2:]
                    subset[j1][-2] += connection_all[k][i][2]
                    subset = np.delete(subset, j2, axis=0)

                # Treat them separately
                else:
                    subset[j1][indexB] = partBs[i]
                    subset[j1][-1] += 1
                    subset[j1][-2] += candidate_scores[partBs[i].astype(int)] + connection_all[k][i][2]

            # Neither point found, create a new subset (if not the last edge)
            elif found == 0 and (add_last_edge or (k < (len(skeleton.edges)-1))):
                row = -1 * np.ones(len(skeleton.nodes)+2)
                row[indexA] = partAs[i] # ID
                row[indexB] = partBs[i] # ID
                row[-1] = 2 # initial count
                row[-2] = sum(candidate_scores[connection_all[k][i, :2].astype(int)]) + connection_all[k][i][2] # score
                subset = np.vstack([subset, row]) # add to matched subset

    # Filter small instances
    score_to_node_ratio = subset[:,-2] / subset[:,-1]
    subset = subset[score_to_node_ratio > min_score_to_node_ratio, :]

    # Done with all the matching! Gather the data
    matched_instances_t = []
    for match in subset:

        # Get teh predicted points for this predicted instance
        pts = {}
        for i, node_name in enumerate(skeleton.node_names):
            if match[i] >= 0:
                match_idx = int(match[i])
                pt = PredictedPoint(x=candidate[match_idx, 0], y=candidate[match_idx, 1],
                                    score=candidate_scores[match_idx])
            else:
                pt = PredictedPoint()
            pts[node_name] = pt

        matched_instances_t.append(PredictedInstance(skeleton=skeleton, points=pts, score=match[-2]))

    return matched_instances_t

def match_peaks_paf(peaks, peak_vals, pafs, skeleton,
                    video, frame_indices, scale=1,
                    min_score_to_node_ratio=0.4, min_score_midpts=0.05,
                    min_score_integral=0.8, add_last_edge=False):
    """ Computes PAF-based peak matching via greedy assignment and other such dragons """

    # Process each frame
    predicted_frames = []
    for peaks_t, peak_vals_t, pafs_t, frame_idx in zip(peaks, peak_vals, pafs, frame_indices):
        instances = match_peaks_frame(peaks_t, peak_vals_t, pafs_t, skeleton, scale,
                                   min_score_to_node_ratio=min_score_to_node_ratio,
                                   min_score_midpts=min_score_midpts,
                                   min_score_integral=min_score_integral,
                                   add_last_edge=add_last_edge,)
        predicted_frames.append(LabeledFrame(video=video, frame_idx=frame_idx, instances=instances))

    return predicted_frames

def match_peaks_paf_par(peaks, peak_vals, pafs, skeleton,
                        video, frame_indices, scale=1,
                        min_score_to_node_ratio=0.4,
                        min_score_midpts=0.05,
                        min_score_integral=0.8,
                        add_last_edge=False,
                        pool=None):
    """ Parallel version of PAF peak matching """

    if pool is None:
        pool = multiprocessing.Pool()

    futures = []
    for peaks_t, peak_vals_t, pafs_t, frame_idx in zip(peaks, peak_vals, pafs, frame_indices):
        future = pool.apply_async(match_peaks_frame, [peaks_t, peak_vals_t, pafs_t, skeleton],
                                  dict(scale=scale,
                                       min_score_to_node_ratio=min_score_to_node_ratio,
                                       min_score_midpts=min_score_midpts,
                                       min_score_integral=min_score_integral,
                                       add_last_edge=add_last_edge))
        futures.append(future)

    predicted_frames = []
    for future, frame_idx in zip(futures, frame_indices):
        instances = future.get()

        # Ok, since we are doing this in parallel. Objects are getting serialized and sent
        # back and forth. This causes all instances to have a different skeleton object
        # This will cause problems later because checking if two skeletons are equal is
        # an expensive operation.
        for i in range(len(instances)):
            points = {node.name: point for node, point in instances[i].nodes_points}
            instances[i] = PredictedInstance(skeleton=skeleton, points=points, score=instances[i].score)

        predicted_frames.append(LabeledFrame(video=video, frame_idx=frame_idx, instances=instances))

    return predicted_frames


@attr.s(auto_attribs=True)
class Predictor:
    """
    The Predictor class takes a trained sLEAP model and runs
    the complete inference pipeline from confidence map/part affinity field
    inference, non-maximum suppression peak finding, paf part matching, to tracking.

    Args:
        model: A trained keras model used for confidence map and paf inference. FIXME: Should this be a keras model or a sLEAP model class
        skeleton: The skeleton(s) to use for prediction. FIXME. This should be stored with the model I think
        inference_batch_size: Frames per inference batch (GPU memory limited)
        read_chunk_size: How many frames to read into CPU memory at a time (CPU memory limited)
        nms_min_thresh: A threshold of non-max suppression peak finding in confidence maps. All
        values below this minimum threshold will be set to zero before peak finding algorithm
        is run.
        nms_sigma: Gaussian blur is applied to confidence maps before non-max supression peak
        finding occurs. This is the standard deviation of the kernel applied to the image.
        min_score_to_node_ratio: FIXME
        min_score_midpts: FIXME
        min_score_integral: FIXME
        add_last_edge: FIXME

    """

    model: keras.Model = attr.ib()
    skeleton: Skeleton = attr.ib()
    inference_batch_size: int = 2
    read_chunk_size: int = 256
    save_frequency: int = 1 # chunks
    nms_min_thresh = 0.3
    nms_sigma = 3
    min_score_to_node_ratio: float = 0.2
    min_score_midpts: float = 0.05
    min_score_integral: float = 0.6
    add_last_edge: bool = True
    with_tracking: bool = False
    flow_window: int = 15
    save_shifted_instances: bool = True

    def predict(self, input_video: Union[str, Video],
                output_path: Optional[str] = None,
                frames: Optional[List[int]] = None) -> List[LabeledFrame]:
        """
        Run the entire inference pipeline on an input video or file object.

        Args:
            input_video: Either a video object or video filename.
            output_path: The output path to save the results.
            frames (optional): List of frames to predict. If None, run entire video.

        Returns:
            list of LabeledFrame objects
        """

        # Load model
        _, h, w, c = self.model.input_shape
        model_channels = c
        logger.info("Loaded models:")
        logger.info("  Input shape: %d x %d x %d" % (h, w, c))

        # Open the video if we need it.
        try:
            input_video.get_frame(0)
            vid = input_video
        except AttributeError:
            vid = Video.from_filename(input_video)

        frames = frames or list(range(vid.num_frames))

        num_frames = len(frames)
        vid_h = vid.shape[1]
        vid_w = vid.shape[2]
        scale = h / vid_h
        logger.info("Opened video:")
        logger.info("  Source: " + str(vid.backend))
        logger.info("  Frames: %d" % num_frames)
        logger.info("  Frame shape: %d x %d" % (vid_h, vid_w))
        logger.info("  Scale: %f" % scale)

        # Initialize tracking
        tracker = FlowShiftTracker(window=self.flow_window, verbosity=0)

        # Initialize parallel pool
        pool = multiprocessing.Pool(processes=usable_cpu_count())

        # Fix the number of threads for OpenCV, not that we are using
        # anything in OpenCV that is actually multi-threaded but maybe
        # we will down the line.
        cv2.setNumThreads(usable_cpu_count())

        # Process chunk-by-chunk!
        t0_start = time()
        predicted_frames: List[LabeledFrame] = []
        num_chunks = int(np.ceil(num_frames / self.read_chunk_size))
        for chunk in range(num_chunks):
            logger.info("Processing chunk %d/%d:" % (chunk + 1, num_chunks))
            t0_chunk = time()

            # Read the next batch of images
            t0 = time()

            # Read the next chunk of frames
            frame_start = self.read_chunk_size * chunk
            frame_end = frame_start + self.read_chunk_size
            if frame_end > num_frames:
                frame_end = num_frames
            frames_idx = frames[frame_start:frame_end]
            mov = vid[frames_idx]

            if (vid.height, vid.width) != (h, w):
                # Resize the frames to the model input size
                scaled_mov = np.zeros((mov.shape[0], h, w, mov.shape[3]))
                for i in range(mov.shape[0]):
                    img = cv2.resize(mov[i, :, :], (w, h))
                     # add back singleton channel
                    if model_channels == 1:
                        img = img[..., None]
                    else:
                        # TODO: figure out when/if this is necessary for RGB videos
                        img = img[..., ::-1]
                    scaled_mov[i, ...] = img
                mov = scaled_mov

            logger.info("  Read %d frames [%.1fs]" % (len(mov), time() - t0))

            # Run inference
            t0 = time()
            confmaps, pafs = self.model.predict(mov.astype("float32") / 255, batch_size=self.inference_batch_size)
            logger.info("  Inferred confmaps and PAFs [%.1fs]" % (time() - t0))

            logger.info(f"  confmaps: shape={confmaps.shape}, ptp={np.ptp(confmaps)}")
            logger.info(f"  pafs: shape={pafs.shape}, ptp={np.ptp(pafs)}")

            # Find peaks
            t0 = time()
            peaks, peak_vals = find_all_peaks(confmaps, min_thresh=self.nms_min_thresh, sigma=self.nms_sigma)
            logger.info("  Found peaks [%.1fs]" % (time() - t0))

            # Match peaks via PAFs
            t0 = time()
            predicted_frames_chunk = match_peaks_paf_par(peaks, peak_vals, pafs, self.skeleton,
                                            scale=scale, video=vid, frame_indices=frames_idx,
                                            min_score_to_node_ratio=self.min_score_to_node_ratio,
                                            min_score_midpts=self.min_score_midpts,
                                            min_score_integral=self.min_score_integral,
                                            add_last_edge=self.add_last_edge, pool=pool)
            logger.info("  Matched peaks via PAFs [%.1fs]" % (time() - t0))

            # Track
            if self.with_tracking:
                t0 = time()
                tracker.process(mov, predicted_frames_chunk)
                logger.info("  Tracked IDs via flow shift [%.1fs]" % (time() - t0))

            # Save
            predicted_frames.extend(predicted_frames_chunk)

            # Get the parameters used for this inference.
            params = attr.asdict(self, filter=lambda attr, value: attr.name not in ["model", "skeleton"])

            if chunk % self.save_frequency == 0 or chunk == (num_chunks - 1):
                t0 = time()

                # FIXME: We are re-writing the whole output each time, this is dumb.
                #  We should save in chunks then combine at the end.
                labels = Labels(labeled_frames=predicted_frames)
                if output_path is not None:
                    Labels.save_json(labels, filename=output_path)

                    logger.info("  Saved to: %s [%.1fs]" % (output_path, time() - t0))

            elapsed = time() - t0_chunk
            total_elapsed = time() - t0_start
            fps = len(predicted_frames) / total_elapsed
            frames_left = num_frames - len(predicted_frames)
            logger.info("  Finished chunk [%.1fs / %.1f FPS / ETA: %.1f min]" % (elapsed, fps, (frames_left / fps) / 60))

            sys.stdout.flush()


        logger.info("Total: %.1f min" % (total_elapsed / 60))

        return predicted_frames

    def run_visual(self, input_video: Union[str, Video], max_frames: int=None):
        """
        Run the confmap/paf prediction on an input video or file object.

        Args:
            input_video: Either a video object or video filename.

        Returns:
            confmaps, pafs: numpy arrays
        """

        # Load model
        _, h, w, c = self.model.input_shape
        model_channels = c
        logger.info("Loaded models:")
        logger.info("  Input shape: %d x %d x %d" % (h, w, c))

        # Open the video if we need it.
        try:
            input_video.get_frame(0)
            vid = input_video
        except AttributeError:
            vid = Video.from_filename(input_video)

        num_frames = max_frames or vid.num_frames
        vid_h = vid.shape[1]
        vid_w = vid.shape[2]
        scale = h / vid_h
        logger.info("Opened video:")
        logger.info("  Source: " + str(vid.backend))
        logger.info("  Frames: %d" % num_frames)
        logger.info("  Frame shape: %d x %d" % (vid_h, vid_w))
        logger.info("  Scale: %f" % scale)

        # Initialize tracking
        # tracker = FlowShiftTracker(window=self.flow_window, verbosity=0)

        # Initialize parallel pool
        pool = multiprocessing.Pool(processes=usable_cpu_count())

        # Fix the number of threads for OpenCV, not that we are using
        # anything in OpenCV that is actually multi-threaded but maybe
        # we will down the line.
        cv2.setNumThreads(usable_cpu_count())

        # Process chunk-by-chunk!
        t0_start = time()
        predicted_frames: List[LabeledFrame] = []
        num_chunks = int(np.ceil(num_frames / self.read_chunk_size))

        confmaps = None
        pafs = None

        for chunk in range(num_chunks):
            logger.info("Processing chunk %d/%d:" % (chunk + 1, num_chunks))
            t0_chunk = time()

            # Read the next batch of images
            t0 = time()

            # Read the next chunk of frames
            frame_start = self.read_chunk_size * chunk
            frame_end = frame_start + self.read_chunk_size
            if frame_end > num_frames:
                frame_end = num_frames
            frames_idx = np.arange(frame_start, frame_end)
            mov = vid[frame_start:frame_end]

            # Preprocess the frames
            if model_channels == 1:
                mov = mov[:, :, :, 0]

            # Resize the frames to the model input size
            for i in range(mov.shape[0]):
                mov[i, :, :] = cv2.resize(mov[i, :, :], (w, h))

            # Add back singleton dimension
            if model_channels == 1:
                mov = mov[..., None]
            else:
                # TODO: figure out when/if this is necessary for RGB videos
                mov = mov[..., ::-1]

            logger.info("  Read %d frames [%.1fs]" % (len(mov), time() - t0))

            # Run inference
            t0 = time()
            batch_confmaps, batch_pafs = self.model.predict(mov.astype("float32") / 255, batch_size=self.inference_batch_size)
            logger.info("  Inferred confmaps and PAFs [%.1fs]" % (time() - t0))

            # Save
            confmaps = batch_confmaps if confmaps is None else np.concatenate((confmaps, batch_confmaps), axis=0)
            pafs = batch_pafs if pafs is None else np.concatenate((pafs, batch_pafs), axis=0)

            elapsed = time() - t0_chunk
            total_elapsed = time() - t0_start

            sys.stdout.flush()


        logger.info("Total: %.1f min" % (total_elapsed / 60))

        return confmaps, pafs

    @classmethod
    def from_training_jobs(cls,
            training_jobs: Dict[ModelOutputType, TrainingJob],
            labels: Optional[Labels]=None):
        """
        Construct a Predictor from some TrainingJobs.

        Args:
            training_jobs: Dict with a TrainingJob for each required ModelOutputType
        Returns:
            Predictor initialized with Keras model.
        """

        # Build the Keras Model
        # This code makes assumptions about the types of TrainingJobs we've been given

        confmap_job = training_jobs[ModelOutputType.CONFIDENCE_MAP]
        pafs_job = training_jobs[ModelOutputType.PART_AFFINITY_FIELD]

        confmap_model_path = os.path.join(confmap_job.save_dir, confmap_job.best_model_filename)
        paf_model_path = os.path.join(pafs_job.save_dir, pafs_job.best_model_filename)

        scale = confmap_job.trainer.scale
        labels = labels or Labels.load_json(confmap_job.labels_filename)
        skeleton = labels.skeletons[0]

        # FIXME: we're now assuming that all the videos are the same size
        vid = labels.videos[0]
        img_shape = (vid.height//scale, vid.width//scale, vid.channels)

        # Load the model
        keras_model = get_inference_model(confmap_model_path, paf_model_path, img_shape)

        # Return the Predictor ready to use
        return cls(model=keras_model, skeleton=skeleton)


def load_predicted_labels_json_old(
        data_path: str, parsed_json: dict = None,
        adjust_matlab_indexing: bool = True,
        fix_rel_paths: bool = True) -> Labels:
    """
    Simple utitlity code to load data from Talmo's old JSON format into newer
    Labels object. This loads the prediced instances

    Args:
        data_path: The path to the JSON file.
        parsed_json: The parsed json if already loaded. Save some time if already parsed.
        adjust_matlab_indexing: Do we need to adjust indexing from MATLAB.
        fix_rel_paths: Fix paths to videos to absolute paths.

    Returns:
        A newly constructed Labels object.
    """
    if parsed_json is None:
        data = json.loads(open(data_path).read())
    else:
        data = parsed_json

    videos = pd.DataFrame(data["videos"])
    predicted_instances = pd.DataFrame(data["predicted_instances"])
    predicted_points = pd.DataFrame(data["predicted_points"])

    if adjust_matlab_indexing:
        predicted_instances.frameIdx -= 1
        predicted_points.frameIdx -= 1

        predicted_points.node -= 1

        predicted_points.x -= 1

        predicted_points.y -= 1

    skeleton = Skeleton()
    skeleton.add_nodes(data["skeleton"]["nodeNames"])
    edges = data["skeleton"]["edges"]
    if adjust_matlab_indexing:
        edges = np.array(edges) - 1
    for (src_idx, dst_idx) in edges:
        skeleton.add_edge(data["skeleton"]["nodeNames"][src_idx], data["skeleton"]["nodeNames"][dst_idx])

    if fix_rel_paths:
        for i, row in videos.iterrows():
            p = row.filepath
            if not os.path.exists(p):
                p = os.path.join(os.path.dirname(data_path), p)
                if os.path.exists(p):
                    videos.at[i, "filepath"] = p

    # Make the video objects
    video_objects = {}
    for i, row in videos.iterrows():
        if videos.at[i, "format"] == "media":
            vid = Video.from_media(videos.at[i, "filepath"])
        else:
            vid = Video.from_hdf5(filename=videos.at[i, "filepath"], dataset=videos.at[i, "dataset"])

        video_objects[videos.at[i, "id"]] = vid

    track_ids = predicted_instances['trackId'].values
    unique_track_ids = np.unique(track_ids)

    spawned_on = {track_id: predicted_instances.loc[predicted_instances['trackId'] == track_id]['frameIdx'].values[0]
                  for track_id in unique_track_ids}
    tracks = {i: Track(name=str(i), spawned_on=spawned_on[i])
              for i in np.unique(predicted_instances['trackId'].values).tolist()}

    # A function to get all the instances for a particular video frame
    def get_frame_predicted_instances(video_id, frame_idx):
        points = predicted_points
        is_in_frame = (points["videoId"] == video_id) & (points["frameIdx"] == frame_idx)
        if not is_in_frame.any():
            return []

        instances = []
        frame_instance_ids = np.unique(points["instanceId"][is_in_frame])
        for i, instance_id in enumerate(frame_instance_ids):
            is_instance = is_in_frame & (points["instanceId"] == instance_id)
            track_id = predicted_instances.loc[predicted_instances['id'] == instance_id]['trackId'].values[0]
            match_score = predicted_instances.loc[predicted_instances['id'] == instance_id]['matching_score'].values[0]
            track_score = predicted_instances.loc[predicted_instances['id'] == instance_id]['tracking_score'].values[0]
            instance_points = {data["skeleton"]["nodeNames"][n]: PredictedPoint(x, y, visible=v, score=confidence)
                               for x, y, n, v, confidence in
                               zip(*[points[k][is_instance] for k in ["x", "y", "node", "visible", "confidence"]])}

            instance = PredictedInstance(skeleton=skeleton,
                                         points=instance_points,
                                         track=tracks[track_id],
                                         score=match_score)
            instances.append(instance)

        return instances

    # Get the unique labeled frames and construct a list of LabeledFrame objects for them.
    frame_keys = list({(videoId, frameIdx) for videoId, frameIdx in zip(predicted_points["videoId"], predicted_points["frameIdx"])})
    frame_keys.sort()
    labels = []
    for videoId, frameIdx in frame_keys:
        label = LabeledFrame(video=video_objects[videoId], frame_idx=frameIdx,
                             instances = get_frame_predicted_instances(videoId, frameIdx))
        labels.append(label)

    return Labels(labels)

def test_visual_inference(args):
    data_path = args.data_path
    confmap_model_path = args.confmap_model_path
    paf_model_path = args.paf_model_path
    save_path = data_path + ".predictions.json"
    skeleton_path = args.skeleton_path

    # Load video
    vid = Video.from_filename(data_path)
    img_shape = (vid.height, vid.width, vid.channels)

    # Load the model
    model = get_inference_model(confmap_model_path, paf_model_path, img_shape)

    # Load the skeleton(s)
    skeleton = Skeleton.load_json(skeleton_path)
    logger.info(f"Skeleton (name={skeleton.name}, {len(skeleton.nodes)} nodes):")

    # Create a predictor to do the work.
    predictor = Predictor(model=model, skeleton=skeleton)

    # Run the inference pipeline
    confmaps, pafs = predictor.run_visual(input_video=vid, max_frames=100)

    print(f"confmaps shape: {confmaps.shape}, pafs shape: {pafs.shape}")

    from PySide2 import QtWidgets
    from sleap.gui.confmapsplot import demo_confmaps
    from sleap.gui.quiverplot import demo_pafs
    app = QtWidgets.QApplication([])
    demo_confmaps(confmaps, vid)
    demo_pafs(pafs, vid)
    app.exec_()

def main(args):
    data_path = args.data_path
    confmap_model_path = args.confmap_model_path
    paf_model_path = args.paf_model_path
    save_path = data_path + ".predictions.json"
    skeleton_path = args.skeleton_path
    frames = args.frames

    if args.resize_input:
        # Load video
        vid = Video.from_filename(data_path)
        img_shape = (vid.height, vid.width, vid.channels)
    else:
        img_shape = None

    # Load the skeleton(s)
    skeleton = Skeleton.load_json(skeleton_path)

    logger.info(f"Skeleton (name={skeleton.name}, {len(skeleton.nodes)} nodes):")

    # Load the model
    model = get_inference_model(confmap_model_path, paf_model_path, img_shape)

    # Create a predictor to do the work.
    predictor = Predictor(model=model, skeleton=skeleton, with_tracking=args.with_tracking)

    # Run the inference pipeline
    return predictor.predict(input_video=data_path, output_path=save_path, frames=frames)


if __name__ == "__main__":

    def frame_list(frame_str):
        return [int(x) for x in frame_str.split(",")] if len(frame_str) else None

    parser = argparse.ArgumentParser()
    parser.add_argument("data_path", help="Path to video file")
    parser.add_argument("confmap_model_path", help="Path to saved confmap model")
    parser.add_argument("paf_model_path", help="Path to saved PAF model")
    parser.add_argument("skeleton_path", help="Path to skeleton file")
    parser.add_argument('--resize-input', dest='resize_input', action='store_const',
                    const=True, default=False,
                    help='resize the input layer to image size (default False)')
    parser.add_argument('--test-viz', dest='test_viz', action='store_const',
                    const=True, default=False,
                    help='just visualize predicted confmaps/pafs (default False)')
    parser.add_argument('--with-tracking', dest='with_tracking', action='store_const',
                    const=True, default=False,
                    help='just visualize predicted confmaps/pafs (default False)')
    parser.add_argument('--frames', type=frame_list, default="", help='list of frames to predict (default is entire video)')

    args = parser.parse_args()

    if args.test_viz:
        test_visual_inference(args)
    else:
        main(args)