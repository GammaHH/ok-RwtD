"""Offline semantic planner. Contains no device or input APIs."""
from copy import deepcopy


def build_plan(actions, specification, observed_features=()):
    """Account for every original action without enabling gameplay execution."""
    observed = set(observed_features)
    bindings = {}
    for group in specification["bindings"]:
        for step in group["steps"]:
            if step in bindings:
                raise ValueError(f"Duplicate binding for step {step}")
            bindings[step] = group
    chest = {step: group for group in specification["chest_sequences"] for step in group["steps"]}
    rows = []
    for step, action in enumerate(actions, 1):
        binding = bindings.get(step)
        feature = binding.get("feature") if binding else None
        role = binding.get("role") if binding else "UNMAPPED"
        blockers = ["DEX_TIMING_TRACE_INCOMPLETE", "PRE_POST_STATE_UNVERIFIED", "ADB_ORIGIN_UNVERIFIED"]
        if binding is None:
            blockers.append("INDIVIDUAL_OPERATION_UNMAPPED")
        elif binding.get("blocked") or binding.get("confidence") == "USER_UNCERTAIN":
            blockers.append("USER_UNCERTAIN")
        if binding and binding.get("irreversible_confirmation_authorized") is False:
            blockers.append("IRREVERSIBLE_ACTION_NOT_AUTHORIZED")
        if feature and feature not in observed:
            blockers.append("FEATURE_NOT_OBSERVED_IN_THIS_SNAPSHOT")
        if step in chest:
            role = "fixed_chest_sequence"
            blockers.append("CHEST_STAGE_FEATURE_MAPPING_PENDING")
        if step == specification["unmapped_death_recovery"]["step"]:
            role = specification["unmapped_death_recovery"]["role"]
            blockers.append("DEATH_PROMPT_COCO_MISSING")
        if step == specification["loading_wait"]["step"]:
            role = "wait_for_loading"
        rows.append({
            "macro": specification["macro"], "step": step,
            "raw_action": deepcopy(action), "feature": feature, "role": role,
            "feature_observed": bool(feature and feature in observed),
            "raw_timing": {key: action.get(key) for key in ("type", "delay", "duration", "cycle", "a_d", "e_o")},
            "normal_chest_step": step in chest,
            "chest_recovery": False if step in chest else None,
            "blockers": blockers, "execution_enabled": False, "device_input_count": 0,
            "note": "Snapshot observations do not prove sequential transitions; original coordinates are evidence only."
        })
    if any(step < 1 or step > len(actions) for step in bindings):
        raise ValueError("Binding step outside original macro")
    return rows
