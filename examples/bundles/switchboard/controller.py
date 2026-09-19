"""Small bundle example that runs the existing Jev Loop kernel under pi-jev."""

from jev_loop import Loop, TaskSpec
from jev_loop.adapters.mock import GoalPolicy, SwitchboardEnvironment, SwitchboardVerifier
from jev_loop.host import LoopController


def build(spec, services, config):
    switches = {str(key): bool(value) for key, value in dict(config["switches"]).items()}
    required = {str(key): bool(value) for key, value in dict(config["required"]).items()}
    environment = SwitchboardEnvironment(switches)
    loop = Loop(
        task=TaskSpec(
            goal=str(spec.task["goal"]),
            inputs=dict(spec.task.get("inputs") or {}),
            constraints=tuple(spec.task.get("constraints") or ()),
            success_criteria=tuple(spec.task.get("success_criteria") or ()),
            authorization=tuple(spec.task.get("authorization") or ()),
        ),
        environment=environment,
        policy=GoalPolicy(required),
        verifier=SwitchboardVerifier(environment, required),
    )
    return LoopController(loop, services, snapshot_extra=lambda: {"world": environment.world})
