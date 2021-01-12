# Copyright (C) 2018 Intel Corporation
#
# SPDX-License-Identifier: MIT
import ast
import datetime
import threading
import time
from zipfile import ZipFile

from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest, QueryDict
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import render
from rest_framework.decorators import api_view

from rules.contrib.views import permission_required, objectgetter
from cvat.apps.authentication.decorators import login_required
from cvat.apps.auto_annotation.models import AnnotationModel
from cvat.apps.engine.models import Task as TaskModel
from cvat.apps.engine.frame_provider import FrameProvider
from cvat.apps.engine.data_manager import TrackManager
from cvat.apps.engine.models import (Job, TrackedShape)
from cvat.apps.engine.serializers import (TrackedShapeSerializer)

from cvat.apps.engine import annotation, task
from cvat.apps.engine.serializers import LabeledDataSerializer
from cvat.apps.engine.annotation import put_task_data,patch_task_data
from tensorflow.python.client import device_lib

import django_rq
import fnmatch
import logging
import copy
import json
import os
import rq

import tensorflow as tf
import numpy as np

from PIL import Image
from cvat.apps.engine.log import slogger
from cvat.settings.base import DATA_ROOT


def load_image_into_numpy(image):
	(im_width, im_height) = image.size
	return np.array(image.getdata()).reshape((im_height, im_width, 3)).astype(np.uint8)


def run_tensorflow_annotation(frame_provider, labels_mapping, threshold, model_path):
    def _normalize_box(box, w, h):
        xmin = int(box[1] * w)
        ymin = int(box[0] * h)
        xmax = int(box[3] * w)
        ymax = int(box[2] * h)
        return xmin, ymin, xmax, ymax

    result = {}
    #use model path provided by user
    # model_path = os.environ.get('TF_ANNOTATION_MODEL_PATH')
    if model_path is None:
        raise OSError('Model path env not found in the system.')
    job = rq.get_current_job()
    #add .pb if default model selected
    if "inference" in model_path:
        if not model_path.endswith('pb'):
            model_path += ".pb"
        detection_graph = tf.Graph()
        with detection_graph.as_default():
            od_graph_def = tf.GraphDef()
            with tf.gfile.GFile(model_path , 'rb') as fid:
                serialized_graph = fid.read()
                od_graph_def.ParseFromString(serialized_graph)
                tf.import_graph_def(od_graph_def, name='')

            try:
                config = tf.ConfigProto()
                config.gpu_options.allow_growth=True
                sess = tf.Session(graph=detection_graph, config=config)
                frames = frame_provider.get_frames(frame_provider.Quality.ORIGINAL)
                for image_num, (image, _) in enumerate(frames):

                    job.refresh()
                    if 'cancel' in job.meta:
                        del job.meta['cancel']
                        job.save()
                        return None
                    job.meta['progress'] = image_num * 100 / len(frame_provider)
                    job.save_meta()

                    image = Image.open(image)
                    width, height = image.size
                    if width > 1920 or height > 1080:
                        image = image.resize((width // 2, height // 2), Image.ANTIALIAS)
                    image_np = load_image_into_numpy(image)
                    image_np_expanded = np.expand_dims(image_np, axis=0)

                    image_tensor = detection_graph.get_tensor_by_name('image_tensor:0')
                    boxes = detection_graph.get_tensor_by_name('detection_boxes:0')
                    scores = detection_graph.get_tensor_by_name('detection_scores:0')
                    classes = detection_graph.get_tensor_by_name('detection_classes:0')
                    num_detections = detection_graph.get_tensor_by_name('num_detections:0')
                    (boxes, scores, classes, num_detections) = sess.run([boxes, scores, classes, num_detections], feed_dict={image_tensor: image_np_expanded})

                    for i in range(len(classes[0])):
                        if classes[0][i] in labels_mapping.keys():
                            if scores[0][i] >= threshold:
                                xmin, ymin, xmax, ymax = _normalize_box(boxes[0][i], width, height)
                                label = labels_mapping[classes[0][i]]
                                if label not in result:
                                    result[label] = []
                                result[label].append([image_num, xmin, ymin, xmax, ymax])
            finally:
                sess.close()
                del sess
    elif "saved_model" in model_path:
        imported_model_v2 = tf.saved_model.load_v2(model_path)
        inference_function = imported_model_v2.signatures["serving_default"]
        with tf.Session() as sess:
            init = tf.global_variables_initializer()
            sess.run(init)
        frames = frame_provider.get_frames(frame_provider.Quality.ORIGINAL)
        for image_num, (image, _) in enumerate(frames):
            job.refresh()
            if 'cancel' in job.meta:
                del job.meta['cancel']
                job.save()
                return None
            job.meta['progress'] = image_num * 100 / len(frame_provider)
            job.save_meta()
            img = tf.io.read_file(image)
            img = tf.image.decode_jpeg(img, channels=3)
            img_shape = tf.shape(img).numpy()
            img = tf.expand_dims(img, axis=0, name='input_tensor')
            inference_result = inference_function(img)
            with tf.Session() as sess:
                inference_result = sess.run(inference_result)
            if(inference_result['num_detections'][0]>0):
                index = 0
                while (index<inference_result['num_detections'][0]) and inference_result['detection_scores'][0][index] >= threshold:
                    if inference_result['detection_classes'][0][index] in labels_mapping.keys():
                        xmin, ymin, xmax, ymax = _normalize_box(inference_result['detection_boxes'][index], img_shape[0], img_shape[1])
                        label = labels_mapping[inference_result['detection_classes'][0][index]]
                        if label not in result:
                            result[label] = []
                        result[label].append([image_num, xmin, ymin, xmax, ymax])
                    index += 1
    return result

def convert_to_cvat_format(data):
	result = {
		"tracks": [],
		"shapes": [],
		"tags": [],
		"version": 0,
	}

	for label in data:
		boxes = data[label]
		for box in boxes:
			result['shapes'].append({
				"type": "rectangle",
				"label_id": label,
				"frame": box[0],
				"points": [box[1], box[2], box[3], box[4]],
				"z_order": 0,
				"group": None,
				"occluded": False,
				"attributes": [],
			})

	return result


def create_thread(tid, labels_mapping, user, tf_annotation_model_path, reset):
    try:
        THRESHOLD = 0.5
        # Init rq job
        job = rq.get_current_job()
        job.meta['progress'] = 0
        job.save_meta()
        # Get job indexes and segment length
        db_task = TaskModel.objects.get(pk=tid)
        # Get image list
        image_list = FrameProvider(db_task.data)

        # Run auto annotation by tf
        result = None
        slogger.glob.info("tf annotation with tensorflow framework for task {}".format(tid))
        result = run_tensorflow_annotation(image_list, labels_mapping, THRESHOLD, tf_annotation_model_path)

        if result is None:
            slogger.glob.info('tf annotation for task {} canceled by user'.format(tid))
            return

        # Modify data format and save
        result = convert_to_cvat_format(result)
        serializer = LabeledDataSerializer(data = result)
        if serializer.is_valid(raise_exception=True):
            if reset:
                put_task_data(tid, user, result)
            else:
                patch_task_data(tid, user, result, "create")

        slogger.glob.info('tf annotation for task {} done'.format(tid))
    except Exception as ex:
        try:
            slogger.task[tid].exception('exception was occured during tf annotation of the task', exc_info=True)
        except:
            slogger.glob.exception('exception was occured during tf annotation of the task {}'.format(tid), exc_info=True)
        raise ex

@api_view(['POST'])
@login_required
def get_meta_info(request):
	try:
		queue = django_rq.get_queue('low')
	
		tids = request.data
		result = {}
		for tid in tids:
			job = queue.fetch_job('tf_annotation.create/{}'.format(tid))
			if job is not None:
				result[tid] = {
					"active": job.is_queued or job.is_started,
					"success": not job.is_failed
				}

		return JsonResponse(result)
	except Exception as ex:
		slogger.glob.exception('exception was occured during tf meta request', exc_into=True)
		return HttpResponseBadRequest(str(ex))


@permission_required(perm=['engine.task.change'],
					 fn=objectgetter(TaskModel, 'tid'), raise_exception=True)
def create(request, tid, mid):
    slogger.glob.info('tf annotation create request for task {}'.format(tid))
    try:
        data = json.loads(request.body.decode('utf-8'))

        user_label_mapping = data["labels"]
        should_reset = data['reset']

        db_task = TaskModel.objects.get(pk=tid)
        queue = django_rq.get_queue('low')
        job = queue.fetch_job('tf_annotation.create/{}'.format(tid))
        if job is not None and (job.is_started or job.is_queued):
            raise Exception("The process is already running")

        db_labels = db_task.label_set.prefetch_related('attributespec_set').all()
        db_labels = {db_label.id:db_label.name for db_label in db_labels}

        if int(mid) == 989898:
            should_reset = True
            tf_model_file_path = os.getenv('TF_ANNOTATION_MODEL_PATH')
            tf_annotation_labels = {
            "person": 1, "bicycle": 2, "car": 3, "motorcycle": 4, "airplane": 5,
            "bus": 6, "train": 7, "truck": 8, "boat": 9, "traffic_light": 10,
            "fire_hydrant": 11, "stop_sign": 13, "parking_meter": 14, "bench": 15,
            "bird": 16, "cat": 17, "dog": 18, "horse": 19, "sheep": 20, "cow": 21,
            "elephant": 22, "bear": 23, "zebra": 24, "giraffe": 25, "backpack": 27,
            "umbrella": 28, "handbag": 31, "tie": 32, "suitcase": 33, "frisbee": 34,
            "skis": 35, "snowboard": 36, "sports_ball": 37, "kite": 38, "baseball_bat": 39,
            "baseball_glove": 40, "skateboard": 41, "surfboard": 42, "tennis_racket": 43,
            "bottle": 44, "wine_glass": 46, "cup": 47, "fork": 48, "knife": 49, "spoon": 50,
            "bowl": 51, "banana": 52, "apple": 53, "sandwich": 54, "orange": 55, "broccoli": 56,
            "carrot": 57, "hot_dog": 58, "pizza": 59, "donut": 60, "cake": 61, "chair": 62,
            "couch": 63, "potted_plant": 64, "bed": 65, "dining_table": 67, "toilet": 70,
            "tv": 72, "laptop": 73, "mouse": 74, "remote": 75, "keyboard": 76, "cell_phone": 77,
            "microwave": 78, "oven": 79, "toaster": 80, "sink": 81, "refrigerator": 83,
            "book": 84, "clock": 85, "vase": 86, "scissors": 87, "teddy_bear": 88, "hair_drier": 89,
            "toothbrush": 90
            }

            labels_mapping = {}
            for key, labels in db_labels.items():
                if labels in tf_annotation_labels.keys():
                    labels_mapping[tf_annotation_labels[labels]] = key
        else:

            dl_model = AnnotationModel.objects.get(pk=mid)

            classes_file_path = dl_model.labelmap_file.name
            tf_model_file_path = dl_model.model_file.name
             # Load and generate the tf annotation labels
            tf_annotation_labels = {}
            with open(classes_file_path, "r") as f:
                f.readline()  # First line is header
                line = f.readline().rstrip()
                cnt = 1
                while line:
                    tf_annotation_labels[line] = cnt
                    line = f.readline().rstrip()
                    cnt += 1

            if len(tf_annotation_labels) == 0:
                raise Exception("No classes found in classes file.")

            labels_mapping = {}
            for tf_class_label, mapped_task_label in user_label_mapping.items():
                for task_label_id, task_label_name in db_labels.items():
                    if task_label_name == mapped_task_label:
                        if tf_class_label in tf_annotation_labels.keys():
                            labels_mapping[tf_annotation_labels[tf_class_label]] = task_label_id

        if not len(labels_mapping.values()):
            raise Exception('No labels found for tf annotation')

        # Run tf annotation job
        queue.enqueue_call(func=create_thread,
            args=(tid, labels_mapping, request.user, tf_model_file_path, should_reset),
            job_id='tf_annotation.create/{}'.format(tid),
            timeout=604800)     # 7 days

        slogger.task[tid].info('tensorflow annotation job enqueued with labels {}'.format(labels_mapping))

    except Exception as ex:
        try:
            slogger.task[tid].exception("exception was occured during tensorflow annotation request", exc_info=True)
        except:
            pass
        return HttpResponseBadRequest(str(ex))

    return HttpResponse()


@login_required
@permission_required(perm=['engine.task.access'],
					 fn=objectgetter(TaskModel, 'tid'), raise_exception=True)
def check(request, tid):
	try:
		queue = django_rq.get_queue('low')
		job = queue.fetch_job('tf_annotation.create/{}'.format(tid))
	
		if job is not None and 'cancel' in job.meta:
			return JsonResponse({'status':'finished'})
	
		data = {}
		if job is None:
			data['status'] = 'unknown'
		elif job.is_queued:
			data['status'] = 'queued'
		elif job.is_started:
			data['status'] = 'started'
			data['progress'] = job.meta['progress']
		elif job.is_finished:
			data['status'] = 'finished'
			job.delete()
		else:
			data['status'] = 'failed'
			data['stderr'] = job.exc_info
			job.delete()

	except Exception:
		data['status'] = 'unknown'

	return JsonResponse(data)


@login_required
@permission_required(perm=['engine.task.change'],
					 fn=objectgetter(TaskModel, 'tid'), raise_exception=True)
def cancel(request, tid):
	try:
		queue = django_rq.get_queue('low')
		job = queue.fetch_job('tf_annotation.create/{}'.format(tid))
	
		if job is None or job.is_finished or job.is_failed:
			raise Exception('Task is not being annotated currently')
		elif 'cancel' not in job.meta:
			job.meta['cancel'] = True
			job.save()
	
	except Exception as ex:
		try:
			slogger.task[tid].exception("cannot cancel tensorflow annotation for task #{}".format(tid), exc_info=True)
		except:
			pass
		return HttpResponseBadRequest(str(ex))

	return HttpResponse()