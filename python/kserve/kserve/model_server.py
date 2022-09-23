# Copyright 2021 The KServe Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
from typing import List, Optional, Dict, Union
import uvicorn
from fastapi import FastAPI
from fastapi.routing import APIRoute as FastAPIRoute
import asyncio

import kserve.handlers as handlers
from tornado.web import RequestHandler

from kserve import Model
from kserve.model_repository import ModelRepository
from ray.serve.api import Deployment, RayServeHandle
from ray import serve
from prometheus_client import REGISTRY
from prometheus_client.exposition import choose_encoder

DEFAULT_HTTP_PORT = 8080
DEFAULT_GRPC_PORT = 8081
DEFAULT_MAX_BUFFER_SIZE = 104857600

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--http_port', default=DEFAULT_HTTP_PORT, type=int,
                    help='The HTTP Port listened to by the model server.')
parser.add_argument('--grpc_port', default=DEFAULT_GRPC_PORT, type=int,
                    help='The GRPC Port listened to by the model server.')
parser.add_argument('--max_buffer_size', default=DEFAULT_MAX_BUFFER_SIZE, type=int,
                    help='The max buffer size for tornado.')
parser.add_argument('--workers', default=1, type=int,
                    help='The number of works to fork')
parser.add_argument('--max_asyncio_workers', default=None, type=int,
                    help='Max number of asyncio workers to spawn')
parser.add_argument(
    "--enable_latency_logging", help="Output a log per request with latency metrics",
    required=False, default=False
)

args, _ = parser.parse_known_args()


class MetricsHandler(RequestHandler):
    def get(self):
        encoder, content_type = choose_encoder(self.request.headers.get('accept'))
        self.set_header("Content-Type", content_type)
        self.write(encoder(REGISTRY))


class ModelServer:
    def __init__(self, http_port: int = args.http_port,
                 grpc_port: int = args.grpc_port,
                 max_buffer_size: int = args.max_buffer_size,
                 workers: int = args.workers,
                 max_asyncio_workers: int = args.max_asyncio_workers,
                 registered_models: ModelRepository = ModelRepository(),
                 enable_latency_logging: bool = args.enable_latency_logging):
        self.registered_models = registered_models
        self.http_port = http_port
        self.grpc_port = grpc_port
        self.max_buffer_size = max_buffer_size
        self.workers = workers
        self.max_asyncio_workers = max_asyncio_workers
        self._http_server = None
        self.enable_latency_logging = validate_enable_latency_logging(enable_latency_logging)

    def create_application(self):
        dataplane = handlers.DataPlane(model_registry=self.registered_models)
        return FastAPI(routes=[
            # Server Liveness API returns 200 if server is alive.
            FastAPIRoute(r"/", dataplane.live),
            FastAPIRoute(r"/v1/models", dataplane.model_metadata),
            # Model Health API returns 200 if model is ready to serve.
            FastAPIRoute(r"/v1/models/{model_name}", dataplane.model_ready),
            FastAPIRoute(r"/v1/models/{model_name}:predict", dataplane.infer, methods=["POST"]),
            FastAPIRoute(r"/v1/models/{model_name}:explain", dataplane.infer, methods=["POST"]),
            FastAPIRoute(r"/v2/repository/models/{model_name}/load", dataplane.load),
            FastAPIRoute(r"/v2/repository/models/{model_name}/unload", dataplane.unload),
        ])

    async def start(self, models: Union[List[Model], Dict[str, Deployment]]):
        if isinstance(models, list):
            for model in models:
                if isinstance(model, Model):
                    self.register_model(model)
                    # pass whether to log request latency into the model
                    model.enable_latency_logging = self.enable_latency_logging
                else:
                    raise RuntimeError("Model type should be Model")
        elif isinstance(models, dict):
            if all([isinstance(v, Deployment) for v in models.values()]):
                serve.start(detached=True, http_options={"host": "0.0.0.0", "port": 9071})
                for key in models:
                    models[key].deploy()
                    handle = models[key].get_handle()
                    self.register_model_handle(key, handle)
            else:
                raise RuntimeError("Model type should be RayServe Deployment")
        else:
            raise RuntimeError("Unknown model collection types")

        cfg = uvicorn.Config(
            self.create_application(),
            port=self.http_port,
            workers=self.workers
        )

        self._server = uvicorn.Server(cfg)
        servers = [self._server.serve()]
        servers_task = asyncio.gather(*servers)
        await servers_task

    def register_model_handle(self, name: str, model_handle: RayServeHandle):
        self.registered_models.update_handle(name, model_handle)
        logging.info("Registering model handle: %s", name)

    def register_model(self, model: Model):
        if not model.name:
            raise Exception(
                "Failed to register model, model.name must be provided.")
        self.registered_models.update(model)
        logging.info("Registering model: %s", model.name)


def validate_enable_latency_logging(enable_latency_logging):
    if isinstance(enable_latency_logging, str):
        if enable_latency_logging.lower() == "true":
            enable_latency_logging = True
        elif enable_latency_logging.lower() == "false":
            enable_latency_logging = False
    if not isinstance(enable_latency_logging, bool):
        raise TypeError(f"enable_latency_logging must be one of [True, true, False, false], "
                        f"got {enable_latency_logging} instead.")
    return enable_latency_logging
