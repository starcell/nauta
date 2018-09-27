#
# INTEL CONFIDENTIAL
# Copyright (c) 2018 Intel Corporation
#
# The source code contained or described herein and all documents related to
# the source code ("Material") are owned by Intel Corporation or its suppliers
# or licensors. Title to the Material remains with Intel Corporation or its
# suppliers and licensors. The Material contains trade secrets and proprietary
# and confidential information of Intel or its suppliers and licensors. The
# Material is protected by worldwide copyright and trade secret laws and treaty
# provisions. No part of the Material may be used, copied, reproduced, modified,
# published, uploaded, posted, transmitted, distributed, or disclosed in any way
# without Intel's prior express written permission.
#
# No license under any patent, copyright, trade secret or other intellectual
# property right is granted to or conferred upon you by disclosure or delivery
# of the Materials, either expressly, by implication, inducement, estoppel or
# otherwise. Any license under such intellectual property rights must be express
# and approved by Intel in writing.
#

import ast
import os
import re
import shutil
from typing import Tuple, List, Optional

import docker
import docker.errors
import yaml
import toml


from util.k8s import k8s_info
from util.logger import initialize_logger
from util.config import FOLDER_DIR_NAME
from util.config import DLS4EConfigMap

import packs.common as common
from util.exceptions import KubectlIntError
import dpath.util as dutil
from cli_text_consts import PACKS_TF_TRAINING_TEXTS as TEXTS


log = initialize_logger('packs.tf_training')


WORK_CNT_PARAM = "workersCount"
P_SERV_CNT_PARAM = "pServersCount"
POD_COUNT_PARAM = "podCount"


def update_configuration(run_folder: str, script_location: str,
                         script_parameters: Tuple[str, ...],
                         experiment_name: str,
                         run_name: str,
                         local_registry_port: int,
                         cluster_registry_port: int,
                         pack_type: str,
                         pack_params: List[Tuple[str, str]],
                         script_folder_location: str = None,
                         env_variables: List[str] = None):
    """
    Updates configuration of a tf-training pack based on paramaters given by a user.

    The following files are modified:
    - Dockerfile - name of a training script is replaced with the one given by a user
                 - all additional files from experiment_folder are copied into an image
                   (excluding files generated by draft)
    - charts/templates/job.yaml - list of arguments is replaces with those given by a user

    :return:
    in case of any errors it throws an exception with a description of a problem
    """
    log.debug("Update configuration - start")

    try:
        modify_values_yaml(run_folder, script_location, script_parameters, pack_params=pack_params,
                           experiment_name=experiment_name, run_name=run_name,
                           pack_type=pack_type, cluster_registry_port=cluster_registry_port,
                           env_variables=env_variables)
        modify_dockerfile(run_folder, script_location, local_registry_port=local_registry_port,
                          script_folder_location=script_folder_location)
        modify_draft_toml(run_folder, registry=f'127.0.0.1:{local_registry_port}')
    except Exception as exe:
        log.exception("Update configuration - i/o error : {}".format(exe))
        raise KubectlIntError(TEXTS["config_not_updated"])

    log.debug("Update configuration - end")


def modify_dockerfile(experiment_folder: str, script_location: str, local_registry_port: int,
                      script_folder_location: str = None):
    log.debug("Modify dockerfile - start")
    dockerfile_name = os.path.join(experiment_folder, "Dockerfile")
    dockerfile_temp_name = os.path.join(experiment_folder, "Dockerfile_Temp")
    dockerfile_temp_content = ""

    with open(dockerfile_name, "r") as dockerfile:
        for line in dockerfile:
            if line.startswith("ADD training.py"):
                if script_location or script_folder_location:
                    dockerfile_temp_content = dockerfile_temp_content + f"COPY {FOLDER_DIR_NAME} ."
            elif line.startswith("FROM dls4e/tensorflow:1.9.0-py"):
                dls4e_config_map = DLS4EConfigMap()
                if line.find('1.9.0-py2') != -1:
                    tf_image_name = dls4e_config_map.py2_image_name
                else:
                    tf_image_name = dls4e_config_map.py3_image_name
                tf_image_repository = f'127.0.0.1:{local_registry_port}/{tf_image_name}'
                dockerfile_temp_content = dockerfile_temp_content + \
                                          f'FROM {tf_image_repository}'

                # pull image from platform's registry
                pull_tf_image(tf_image_repository=tf_image_repository)
            else:
                dockerfile_temp_content = dockerfile_temp_content + line

    with open(dockerfile_temp_name, "w") as dockerfile_temp:
        dockerfile_temp.write(dockerfile_temp_content)

    shutil.move(dockerfile_temp_name, dockerfile_name)
    log.debug("Modify dockerfile - end")


def modify_values_yaml(experiment_folder: str, script_location: str, script_parameters: Tuple[str, ...],
                       experiment_name: str, run_name: str, pack_type: str,
                       cluster_registry_port: int, pack_params: List[Tuple[str, str]],
                       env_variables: List[str]):
    log.debug("Modify values.yaml - start")
    values_yaml_filename = os.path.join(experiment_folder, f"charts/{pack_type}/values.yaml")
    values_yaml_temp_filename = os.path.join(experiment_folder, f"charts/{pack_type}/values_temp.yaml")

    with open(values_yaml_filename, "r") as values_yaml_file:
        v = yaml.load(values_yaml_file)

        if "commandline" in v:
            v["commandline"]["args"] = common.prepare_script_paramaters(script_parameters, script_location)
        v["experimentName"] = experiment_name
        v["registry_port"] = str(cluster_registry_port)
        v["image"]["clusterRepository"] = f'127.0.0.1:{cluster_registry_port}/{run_name}'
        regex = re.compile("^\[.*|^\{.*")

        workersCount = None
        pServersCount = None

        for key, value in pack_params:
            if re.match(regex, value):
                try:
                    value = ast.literal_eval(value)
                except Exception as e:
                    raise AttributeError(TEXTS["cant_parse_value"].format(value=value, error=e))
            if key == WORK_CNT_PARAM:
                workersCount = value
            if key == P_SERV_CNT_PARAM:
                pServersCount = value

            dutil.new(v, key, value, '.')

        # setting sum of replicas involved in multinode training if both pServersCount and workersCount are present in
        # the pack or given in the cli
        if (WORK_CNT_PARAM in v or workersCount) and (P_SERV_CNT_PARAM in v or pServersCount):
            number_of_replicas = int(v.get(WORK_CNT_PARAM)) if not workersCount else int(workersCount)
            number_of_replicas += int(v.get(P_SERV_CNT_PARAM)) if not pServersCount else int(pServersCount)
            v[POD_COUNT_PARAM] = number_of_replicas

        if env_variables:
            env_list = []
            for variable in env_variables:
                key, value = variable.split("=")

                one_env_map = {"name": key, "value": value}

                env_list.append(one_env_map)
            if v.get("env"):
                v["env"].extend(env_list)
            else:
                v["env"] = env_list

    with open(values_yaml_temp_filename, "w") as values_yaml_file:
        yaml.dump(v, values_yaml_file)

    shutil.move(values_yaml_temp_filename, values_yaml_filename)
    log.debug("Modify values.yaml - end")


def modify_draft_toml(experiment_folder: str, registry: str):
    log.debug("Modify draft.toml - start")
    draft_toml_filename = os.path.join(experiment_folder, "draft.toml")
    draft_toml_temp_filename = os.path.join(experiment_folder, "draft_temp.toml")
    namespace = k8s_info.get_kubectl_current_context_namespace()

    with open(draft_toml_filename, "r") as draft_toml_file:
        draft_toml_yaml = toml.load(draft_toml_file)

    log.debug(draft_toml_yaml["environments"])
    draft_toml_yaml["environments"]["development"]["namespace"] = namespace
    draft_toml_yaml["environments"]["development"]["registry"] = registry


    with open(draft_toml_temp_filename, "w") as draft_toml_file:
        toml.dump(draft_toml_yaml, draft_toml_file)

    shutil.move(draft_toml_temp_filename, draft_toml_filename)
    log.debug("Modify draft.toml - end")


def pull_tf_image(tf_image_repository: str):
    try:
        log.debug(f'Pulling TF image: {tf_image_repository}')
        docker_client = docker.from_env()
        docker_client.images.pull(repository=tf_image_repository)
    except docker.errors.APIError:
        log.exception(f'Failed to pull TF image: {tf_image_repository}')


def get_pod_count(run_folder: str, pack_type: str) -> Optional[int]:
    log.debug(f"Getting pod count for Run: {run_folder}")
    values_yaml_filename = os.path.join(run_folder, f"charts/{pack_type}/values.yaml")

    with open(values_yaml_filename, "r") as values_yaml_file:
        values = yaml.load(values_yaml_file)

    pod_count = values.get(POD_COUNT_PARAM)

    log.debug(f"Pod count for Run: {run_folder} = {pod_count}")

    return int(pod_count) if pod_count else None
