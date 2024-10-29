import os
import json
from collections import defaultdict
import logging
from typing import List, Optional

from maya import cmds
import ayon_api
import attr

from ayon_core.pipeline import get_current_project_name
from ayon_maya import api

from . import lib
from .alembic import get_alembic_ids_cache
from .usd import is_usd_lib_supported, get_usd_ids_cache


log = logging.getLogger(__name__)


ATTRIBUTE_MAPPING = {
    "primaryVisibility": "visibility",  # Camera
    "castsShadows": "visibility",  # Shadow
    "receiveShadows": "receive_shadows",
    "aiSelfShadows": "self_shadows",
    "aiOpaque": "opaque",
    "aiMatte": "matte",
    "aiVisibleInDiffuseTransmission": "visibility",
    "aiVisibleInSpecularTransmission": "visibility",
    "aiVisibleInVolume": "visibility",
    "aiVisibleInDiffuseReflection": "visibility",
    "aiVisibleInSpecularReflection": "visibility",
    "aiSubdivUvSmoothing": "subdiv_uv_smoothing",
    "aiDispHeight": "disp_height",
    "aiDispPadding": "disp_padding",
    "aiDispZeroValue": "disp_zero_value",
    "aiStepSize": "step_size",
    "aiVolumePadding": "volume_padding",
    "aiSubdivType": "subdiv_type",
    "aiSubdivIterations": "subdiv_iterations"
}


def calculate_visibility_mask(attributes):
    # https://arnoldsupport.com/2018/11/21/backdoor-setting-visibility/
    mapping = {
        "primaryVisibility": 1,  # Camera
        "castsShadows": 2,  # Shadow
        "aiVisibleInDiffuseTransmission": 4,
        "aiVisibleInSpecularTransmission": 8,
        "aiVisibleInVolume": 16,
        "aiVisibleInDiffuseReflection": 32,
        "aiVisibleInSpecularReflection": 64
    }
    mask = 255
    for attr_name, value in mapping.items():
        if attributes.get(attr_name, True):
            continue

        mask -= value

    return mask


def get_nodes_by_id(standin):
    """Get node id from aiStandIn via json sidecar.

    Args:
        standin (string): aiStandIn node.

    Returns:
        (dict): Dictionary with node full name/path and id.
    """
    path = cmds.getAttr(standin + ".dso")

    if path.endswith(".abc"):
        # Support alembic files directly
        return get_alembic_ids_cache(path)

    elif (
        is_usd_lib_supported and
        any(path.endswith(ext) for ext in [".usd", ".usda", ".usdc"])
    ):
        # Support usd files directly
        return get_usd_ids_cache(path)

    json_path = None
    for f in os.listdir(os.path.dirname(path)):
        if f.endswith(".json"):
            json_path = os.path.join(os.path.dirname(path), f)
            break

    if not json_path:
        log.warning("Could not find json file for {}.".format(standin))
        return {}

    with open(json_path, "r") as f:
        return json.load(f)


def shading_engine_assignments(shading_engine, attribute, nodes, assignments):
    """Full assignments with shader or disp_map.

    Args:
        shading_engine (string): Shading engine for material.
        attribute (string): "surfaceShader" or "displacementShader"
        nodes: (list): Nodes paths relative to aiStandIn.
        assignments (dict): Assignments by nodes.

    Returns:
        dict[str, list[str]]: The operator `aiSetParameter` assignments
          needed per node to assign the shading engine.

    """
    shader_inputs = cmds.listConnections(
        shading_engine + "." + attribute, source=True
    )
    if not shader_inputs:
        log.info(
            "Shading engine \"{}\" missing input \"{}\"".format(
                shading_engine, attribute
            )
        )
        return

    # Strip off component assignments
    for i, node in enumerate(nodes):
        if "." in node:
            log.warning(
                "Converting face assignment to full object assignment. This "
                "conversion can be lossy: {}".format(node)
            )
            nodes[i] = node.split(".")[0]

    shader_type = "shader" if attribute == "surfaceShader" else "disp_map"
    assignment = "{}='{}'".format(shader_type, shader_inputs[0])
    for node in nodes:
        assignments[node].append(assignment)


@attr.s
class SetParameter:
    """Simple class to manage aiSetParameter nodes"""
    selection: str = attr.ib()
    assignments: List[str] = attr.ib()
    node: Optional[str] = attr.ib(default=None)

    def create(self, name=None) -> str:
        operator: str = cmds.createNode("aiSetParameter",
                                        skipSelect=True,
                                         name=name)
        self.node = operator
        self.update()
        return operator

    def update(self):
        operator = self.node
        cmds.setAttr(f"{operator}.selection", self.selection, type="string")

        # Remove any existing assignments
        for i in reversed(
            range(cmds.getAttr(f"{operator}.assignment", size=True))
        ):
            cmds.removeMultiInstance(f"{operator}.assignment[{i}]", b=True)

        # Set the new assignments
        for i, assignment in enumerate(self.assignments):
            cmds.setAttr(
                f"{operator}.assignment[{i}]",
                assignment,
                type="string"
            )

    def delete(self):
        if self.node and cmds.objExists(self.node):
            cmds.delete(self.node)


def get_current_set_parameter_operators(standin: str) -> List[SetParameter]:
    """Return SetParameter operators for a aiStandin node.

    Args:
        standin: The `aiStandin` node to get the assignments from.

    Returns:
        The list of `SetParameter` objects that represent the assignments.

    """
    plug = standin + ".operators"
    num = cmds.getAttr(plug, size=True)

    set_parameters = []
    for i in range(num):

        index_plug = f"{plug}[{i}]"

        inputs = cmds.listConnections(
            index_plug, source=True, destination=False)
        if not inputs:
            continue

        # We only consider `aiSetParameter` nodes for now because that is what
        # the look assignment logic creates.
        input_node = inputs[0]
        if cmds.nodeType(input_node) != "aiSetParameter":
            continue

        selection = cmds.getAttr(f"{input_node}.selection")
        assignment_plug = f"{input_node}.assignment"
        assignments = []
        for j in range(cmds.getAttr(assignment_plug, size=True)):
            assignment_index_plug = f"{assignment_plug}[{j}]"
            assignment = cmds.getAttr(assignment_index_plug)
            assignments.append(assignment)

        parameter = SetParameter(
            selection=selection,
            assignments=assignments,
            node=input_node)
        set_parameters.append(parameter)
    return set_parameters


def assign_look(
        standin: str,
        product_name: str,
        include_selection_prefixes: Optional[List[str]] = None):
    """Assign a look to an aiStandIn node.

    Arguments:
        standin (str): The aiStandin proxy shape.
        product_name (str): The product name to load.
        include_selection_prefixes (Optional[List[str]]): If provided,
            only children to these object path prefixes will be considered.
            The paths are the full path from the root of the Alembic file,
            e.g. `/parent/child1/child2`
    """
    # TODO: Technically a more logical entry point here to assign by version
    #  instead of product name so that you can also assign older looks.
    log.info("Assigning {} to {}.".format(product_name, standin))

    nodes_by_id = get_nodes_by_id(standin)

    # If any inclusion selection prefixes are set we allow assigning only
    # to those paths or any children
    if include_selection_prefixes:
        prefixes = tuple(f"{prefix}/" for prefix in include_selection_prefixes)
        for node_id, nodes in dict(nodes_by_id).items():
            print(nodes)
            nodes = [node for node in nodes if node.startswith(prefixes)]
            if nodes:
                nodes_by_id[node_id] = nodes
            else:
                nodes_by_id.pop(node_id)

    # Group by folder id so we run over the look per folder
    node_ids_by_folder_id = defaultdict(set)
    for node_id in nodes_by_id:
        folder_id = node_id.split(":", 1)[0]
        node_ids_by_folder_id[folder_id].add(node_id)

    project_name = get_current_project_name()

    operators = get_current_set_parameter_operators(standin)
    operators_by_node = {
        cmds.getAttr(f"{op.node}.selection"): op for op in operators
    }

    for folder_id, node_ids in node_ids_by_folder_id.items():

        # Get latest look version
        version_entity = ayon_api.get_last_version_by_product_name(
            project_name,
            product_name,
            folder_id,
            fields={"id"}
        )
        if not version_entity:
            log.info("Didn't find last version for product name {}".format(
                product_name
            ))
            continue
        version_id = version_entity["id"]

        relationships = lib.get_look_relationships(version_id)
        shader_nodes, container_node = lib.load_look(version_id)
        namespace = shader_nodes[0].split(":")[0]

        # Get only the node ids and paths related to this folder
        # And get the shader edits the look supplies
        asset_nodes_by_id = {
            node_id: nodes_by_id[node_id] for node_id in node_ids
        }
        edits = list(
            api.lib.iter_shader_edits(
                relationships, shader_nodes, asset_nodes_by_id
            )
        )

        # Define the assignment operators needed for this look
        node_assignments = {}
        for edit in edits:
            for node in edit["nodes"]:
                if node not in node_assignments:
                    node_assignments[node] = []

            if edit["action"] == "assign":
                if not cmds.ls(edit["shader"], type="shadingEngine"):
                    log.info("Skipping non-shader: %s" % edit["shader"])
                    continue

                shading_engine_assignments(
                    shading_engine=edit["shader"],
                    attribute="surfaceShader",
                    nodes=edit["nodes"],
                    assignments=node_assignments
                )
                shading_engine_assignments(
                    shading_engine=edit["shader"],
                    attribute="displacementShader",
                    nodes=edit["nodes"],
                    assignments=node_assignments
                )

            if edit["action"] == "setattr":
                visibility = False
                for attr_name, value in edit["attributes"].items():
                    if attr_name not in ATTRIBUTE_MAPPING:
                        log.warning(
                            "Skipping setting attribute {} on {} because it is"
                            " not recognized.".format(attr_name, edit["nodes"])
                        )
                        continue

                    if isinstance(value, str):
                        value = "'{}'".format(value)

                    mapped_attr_name = ATTRIBUTE_MAPPING[attr_name]
                    if mapped_attr_name == "visibility":
                        visibility = True
                        continue

                    assignment = f"{mapped_attr_name}={value}"
                    for node in edit["nodes"]:
                        node_assignments[node].append(assignment)

                if visibility:
                    mask = calculate_visibility_mask(edit["attributes"])
                    assignment = "visibility={}".format(mask)

                    for node in edit["nodes"]:
                        node_assignments[node].append(assignment)

        # Cleanup: remove any empty operator slots
        plug = standin + ".operators"
        num = cmds.getAttr(plug, size=True)
        for i in reversed(range(num)):
            index_plug = f"{plug}[{i}]"
            if not cmds.listConnections(index_plug,
                                        source=True,
                                        destination=False):
                cmds.removeMultiInstance(index_plug, b=True)

        # Update the node assignments on the standin
        for node, assignments in node_assignments.items():
            if not assignments:
                continue

            # If this node has an existing assignment, update it
            if node in operators_by_node:
                set_parameter = operators_by_node[node]
                set_parameter.assignments[:] = assignments
                set_parameter.update()

            # Create a new assignment
            else:
                set_parameter = SetParameter(
                    selection=node,
                    assignments=assignments
                )
                operators_by_node[node] = set_parameter

                # Create the `aiSetParameter` node
                label = node.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
                name = f"{namespace}:set_parameter_{label}"
                operator = set_parameter.create(name=name)

                # Connect to next available index
                size = cmds.getAttr(plug, size=True)
                cmds.connectAttr(
                    f"{operator}.out",
                    f"{plug}[{size}]",
                    force=True
                )

                # Add it to the looks container so it is removed along
                # with it if needed.
                cmds.sets(operator, edit=True, addElement=container_node)
