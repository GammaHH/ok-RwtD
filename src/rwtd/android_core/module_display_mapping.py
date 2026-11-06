"""Module layout corrections in reference space; saved old-phone data stays intact."""
import copy
import json

from .workflow_model import validate_roi


class ModuleDisplayMapping:
    def __init__(self,root):
        from .user_data import definition_root
        path=definition_root(root)/'assets/rwtd/scheduler/module_display_mapping.json'
        data=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {
            'schema_version':1,'profiles':{}}
        if set(data)!={'schema_version','profiles'} or data['schema_version']!=1 or not isinstance(data['profiles'],dict):
            raise ValueError('Invalid module display mapping')
        from .module_store import identifier
        from .screen_geometry import expected_size
        for size,rules in data['profiles'].items():
            if expected_size(size) is None or not isinstance(rules,dict):
                raise ValueError('Invalid mapping display profile')
            for module,rule in rules.items():
                identifier(module)
                if (not isinstance(rule,dict) or set(rule)!={'offset_y','point_exempt_steps'}
                        or type(rule['offset_y']) is not int or abs(rule['offset_y'])>200
                        or not isinstance(rule['point_exempt_steps'],list)
                        or any(not isinstance(step,str) or not step for step in rule['point_exempt_steps'])):
                    raise ValueError('Invalid module display rule')
        # Read once per task/service start, never scan config on each retry.
        self.profiles=data['profiles']
        from .resolution_calibration import CalibrationStore
        self.calibration = CalibrationStore(root)
        # Frozen for this native task run; saves require stopping the owner first.
        self.calibrations = self.calibration.load()['profiles']

    def calibrated_points(self, task, module, parameters):
        if not any(module in rules for rules in self.calibrations.values()):
            return False
        baseline=copy.copy(self)
        baseline.calibrations={}
        return self.calibration.has_points(self.calibrations, task.screen_geometry(), module,
                                          parameters, baseline.apply(task,module,parameters))

    def describe(self,geometry):
        if geometry is None:
            return {}
        return copy.deepcopy(self.profiles.get(f'{geometry.width}x{geometry.height}',{}))

    def apply(self,task,module,parameters):
        result=copy.deepcopy(parameters)
        if not any(module in rules for rules in (*self.profiles.values(), *self.calibrations.values())):
            return result
        geometry=task.screen_geometry()
        rule=self.describe(geometry).get(module)
        if not rule:
            return self.calibration.apply(self.calibrations,geometry,module,parameters,result)
        offset=rule['offset_y']
        point_exempt=str(result.get('step_id')) in rule['point_exempt_steps']
        if not point_exempt:
            for x_key,y_key in (('x','y'),('mid_x','mid_y'),('end_x','end_y')):
                if x_key in result and y_key in result:
                    result[y_key]+=offset
                    if not 0<=result[y_key]<2400:
                        raise ValueError('Mapped touch outside reference canvas')

        visited=set()
        def shift(roi):
            if id(roi) in visited:
                return
            visited.add(id(roi))
            validate_roi(roi)
            roi[1]+=offset
            validate_roi(roi)

        def regions(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if (key in {'roi','coco_roi','ocr_roi','color_roi','global_roi'}
                            and isinstance(item,list)):
                        shift(item)
                    elif key=='coco_feature_rois' and isinstance(item,dict):
                        for roi in item.values():
                            if roi is not None:
                                shift(roi)
                    else:
                        regions(item)
            elif isinstance(value,list):
                for item in value:
                    regions(item)
        regions(result)
        logger=getattr(task,'_log_template_mapping',None)
        if callable(logger):
            logger(kind='module_layout',module=module,step_id=result.get('step_id'),
                   offset_y=offset,point_exempt=point_exempt,
                   source_size=[geometry.width,geometry.height])
        return self.calibration.apply(self.calibrations,geometry,module,parameters,result)
