
# Copyright (C) 2020 Intel Corporation
#
# SPDX-License-Identifier: MIT

class _GitImportFix:
    import sys
    former_path = sys.path[:]

    @classmethod
    def apply(cls):
        # HACK: fix application and module name clash
        # 'git' app is found earlier than a library in the path.
        # The clash is introduced by unittest discover
        import sys
        print('apply')

        apps_dir = __file__[:__file__.rfind('/dataset_manager/')]
        assert 'apps' in apps_dir
        try:
            sys.path.remove(apps_dir)
        except ValueError:
            pass

        for name in list(sys.modules):
            if name.startswith('git.') or name == 'git':
                m = sys.modules.pop(name, None)
                del m

        import git
        assert apps_dir not in git.__file__

    @classmethod
    def restore(cls):
        import sys
        print('restore')

        for name in list(sys.modules):
            if name.startswith('git.') or name == 'git':
                m = sys.modules.pop(name)
                del m

        sys.path.insert(0, __file__[:__file__.rfind('/dataset_manager/')])

        import importlib
        importlib.invalidate_caches()

def _setUpModule():
    _GitImportFix.apply()
    import cvat.apps.dataset_manager.task as dm
    from cvat.apps.engine.models import Task
    globals()['dm'] = dm
    globals()['Task'] = Task

    import sys
    sys.path.insert(0, __file__[:__file__.rfind('/dataset_manager/')])

def tearDownModule():
    _GitImportFix.restore()

from io import BytesIO
import os
import random
import tempfile

from PIL import Image
from django.contrib.auth.models import User, Group
from rest_framework.test import APITestCase, APIClient
from rest_framework import status

_setUpModule()


def generate_image_file(filename):
    f = BytesIO()
    width = random.randint(10, 200)
    height = random.randint(10, 200)
    image = Image.new('RGB', size=(width, height))
    image.save(f, 'jpeg')
    f.name = filename
    f.seek(0)

    return f

def create_db_users(cls):
    group_user, _ = Group.objects.get_or_create(name="user")

    user_dummy = User.objects.create_superuser(username="test", password="test", email="")
    user_dummy.groups.add(group_user)

    cls.user = user_dummy

class ForceLogin:
    def __init__(self, user, client):
        self.user = user
        self.client = client

    def __enter__(self):
        if self.user:
            self.client.force_login(self.user,
                backend='django.contrib.auth.backends.ModelBackend')

        return self

    def __exit__(self, exception_type, exception_value, traceback):
        if self.user:
            self.client.logout()

class TaskExportTest(APITestCase):
    def setUp(self):
        self.client = APIClient()

    @classmethod
    def setUpTestData(cls):
        create_db_users(cls)

    def _generate_task(self):
        task = {
            "name": "my task #1",
            "owner": '',
            "assignee": '',
            "overlap": 0,
            "segment_size": 100,
            "z_order": False,
            "labels": [
                {
                    "name": "car",
                    "attributes": [
                        {
                            "name": "model",
                            "mutable": False,
                            "input_type": "select",
                            "default_value": "mazda",
                            "values": ["bmw", "mazda", "renault"]
                        },
                        {
                            "name": "parked",
                            "mutable": True,
                            "input_type": "checkbox",
                            "default_value": False
                        },
                    ]
                },
                {"name": "person"},
            ]
        }
        task = self._create_task(task, 3)

        annotations = {
            "version": 0,
            "tags": [
                {
                    "frame": 0,
                    "label_id": task["labels"][0]["id"],
                    "group": None,
                    "attributes": []
                }
            ],
            "shapes": [
                {
                    "frame": 0,
                    "label_id": task["labels"][0]["id"],
                    "group": None,
                    "attributes": [
                        {
                            "spec_id": task["labels"][0]["attributes"][0]["id"],
                            "value": task["labels"][0]["attributes"][0]["values"][0]
                        },
                        {
                            "spec_id": task["labels"][0]["attributes"][1]["id"],
                            "value": task["labels"][0]["attributes"][0]["default_value"]
                        }
                    ],
                    "points": [1.0, 2.1, 100, 300.222],
                    "type": "rectangle",
                    "occluded": False
                },
                {
                    "frame": 1,
                    "label_id": task["labels"][1]["id"],
                    "group": None,
                    "attributes": [],
                    "points": [2.0, 2.1, 100, 300.222, 400, 500, 1, 3],
                    "type": "polygon",
                    "occluded": False
                },
            ],
            "tracks": [
                {
                    "frame": 0,
                    "label_id": task["labels"][0]["id"],
                    "group": None,
                    "attributes": [
                        {
                            "spec_id": task["labels"][0]["attributes"][0]["id"],
                            "value": task["labels"][0]["attributes"][0]["values"][0]
                        },
                    ],
                    "shapes": [
                        {
                            "frame": 0,
                            "points": [1.0, 2.1, 100, 300.222],
                            "type": "rectangle",
                            "occluded": False,
                            "outside": False,
                            "attributes": [
                                {
                                    "spec_id": task["labels"][0]["attributes"][1]["id"],
                                    "value": task["labels"][0]["attributes"][1]["default_value"]
                                }
                            ]
                        },
                        {
                            "frame": 1,
                            "attributes": [],
                            "points": [2.0, 2.1, 100, 300.222],
                            "type": "rectangle",
                            "occluded": True,
                            "outside": True
                        },
                    ]
                },
                {
                    "frame": 1,
                    "label_id": task["labels"][1]["id"],
                    "group": None,
                    "attributes": [],
                    "shapes": [
                        {
                            "frame": 1,
                            "attributes": [],
                            "points": [1.0, 2.1, 100, 300.222],
                            "type": "rectangle",
                            "occluded": False,
                            "outside": False
                        }
                    ]
                },
            ]
        }
        self._put_api_v1_task_id_annotations(task["id"], annotations)

        return task, annotations

    def _create_task(self, data, size):
        with ForceLogin(self.user, self.client):
            response = self.client.post('/api/v1/tasks', data=data, format="json")
            assert response.status_code == status.HTTP_201_CREATED, response.status_code
            tid = response.data["id"]

            images = {
                "client_files[%d]" % i: generate_image_file("image_%d.jpg" % i)
                for i in range(size)
            }
            images["image_quality"] = 75
            response = self.client.post("/api/v1/tasks/{}/data".format(tid), data=images)
            assert response.status_code == status.HTTP_202_ACCEPTED, response.status_code

            response = self.client.get("/api/v1/tasks/{}".format(tid))
            task = response.data

        return task

    def _put_api_v1_task_id_annotations(self, tid, data):
        with ForceLogin(self.user, self.client):
            response = self.client.put("/api/v1/tasks/{}/annotations".format(tid),
                data=data, format="json")

        return response

    def _test_export(self, format_name, save_images=False):
        self.assertTrue(format_name in [f['tag'] for f in dm.EXPORT_FORMATS])

        task, _ = self._generate_task()
        project = dm.TaskProject.from_task(
            Task.objects.get(pk=task["id"]), self.user.username)

        with tempfile.TemporaryDirectory() as test_dir:
            project.export(format_name, test_dir, save_images=save_images)

            self.assertTrue(os.listdir(test_dir))

    def test_datumaro(self):
        self._test_export(dm.EXPORT_FORMAT_DATUMARO_PROJECT, save_images=False)

    def test_coco(self):
        self._test_export('cvat_coco', save_images=True)

    def test_voc(self):
        self._test_export('cvat_voc', save_images=True)

    def test_tf_detection_api(self):
        self._test_export('cvat_tfrecord', save_images=True)

    def test_yolo(self):
        self._test_export('cvat_yolo', save_images=True)

    def test_mot(self):
        self._test_export('cvat_mot', save_images=True)

    def test_labelme(self):
        self._test_export('cvat_label_me', save_images=True)

    def test_formats_query(self):
        formats = dm.get_export_formats()

        expected = set(f['tag'] for f in dm.EXPORT_FORMATS)
        actual = set(f['tag'] for f in formats)
        self.assertSetEqual(expected, actual)
