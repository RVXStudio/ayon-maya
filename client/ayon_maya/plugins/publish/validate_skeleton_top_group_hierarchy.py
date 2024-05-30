# -*- coding: utf-8 -*-
"""Plugin for validating naming conventions."""
from ayon_core.pipeline.publish import (
    OptionalPyblishPluginMixin,
    PublishValidationError,
    ValidateContentsOrder,
)
from ayon_maya.api.plugin import MayaInstancePlugin
from maya import cmds


class ValidateSkeletonTopGroupHierarchy(MayaInstancePlugin,
                                        OptionalPyblishPluginMixin):
    """Validates top group hierarchy in the SETs
    Make sure the object inside the SETs are always top
    group of the hierarchy

    """
    order = ValidateContentsOrder + 0.05
    label = "Skeleton Rig Top Group Hierarchy"
    families = ["rig.fbx"]
    optional = True

    def process(self, instance):
        if not self.is_active(instance.data):
            return

        invalid = []
        skeleton_mesh_data = instance.data("skeleton_mesh", [])
        if skeleton_mesh_data:
            invalid = self.get_top_hierarchy(skeleton_mesh_data)
            if invalid:
                raise PublishValidationError(
                    "The skeletonMesh_SET includes the object which "
                    "is not at the top hierarchy: {}".format(invalid))

    def get_top_hierarchy(self, targets):
        targets = cmds.ls(targets, long=True)  # ensure long names
        non_top_hierarchy_list = [
            target for target in targets if target.count("|") > 2
        ]
        return non_top_hierarchy_list
