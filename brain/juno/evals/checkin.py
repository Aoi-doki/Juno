"""Does this model know when to shut up?

Juno's check-in prompt asks the model to look at recent activity and reply
``SILENCE`` when there's nothing worth saying — which should be most of the
time. That instruction asks a model to *suppress* a helpful impulse, and it is
exactly what eagerness-tuned models are worst at. A model that speaks on 60% of
check-ins instead of 10% makes Juno intolerable within a day.

No benchmark measures restraint, so this measures it directly. Run it against
each engine and choose from the table rather than from vibes:

    python -m juno.evals.checkin --engine gemini --engine local

The two error types are not equally bad and are reported separately:

**False alarms** — speaking when it should have stayed quiet. These are what get
Juno uninstalled. Weight them heavily.

**Misses** — staying quiet when it should have spoken. Annoying but survivable;
the deterministic rules (calendar, scroll thresholds) still fire regardless,
since those never consult a model about *whether* to speak.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

from juno.config import Config, ModelConfig
from juno.engines import EngineError, build_engine
from juno.memory import Memory
from juno.proactive import CHECK_IN_PROMPT, is_silence


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    digest: str
    should_speak: bool
    plan: str | None = None
    note: str = ""


# Deliberately more quiet cases than loud ones, because that is the real ratio.
# A model that speaks every time scores 0% here rather than 50%.
SCENARIOS: list[Scenario] = [
    # --- should stay quiet ---
    Scenario(
        "on task",
        "09:12  Ghostty — nvim juno/agent.py  (48m)",
        should_speak=False,
        plan="writing the engine adapter",
    ),
    Scenario(
        "short break",
        "10:02  Ghostty — nvim  (52m)\n10:54  Firefox — news.ycombinator.com  (3m)",
        should_speak=False,
        plan="writing the engine adapter",
        note="Three minutes is a breath, not a derailment.",
    ),
    Scenario(
        "reading docs for the task",
        "14:00  Ghostty — nvim  (20m)\n14:20  Firefox — developer.android.com  (25m)",
        should_speak=False,
        plan="the Android service",
        note="Looks like drift, is actually the work.",
    ),
    Scenario("nothing recorded", "(nothing recorded)", should_speak=False),
    Scenario(
        "away from the desk",
        "12:30  away from the desk  (40m)",
        should_speak=False,
        note="Nobody is there to hear it.",
    ),
    Scenario(
        "ordinary varied work, no stated plan",
        "13:00  Slack  (12m)\n13:12  Ghostty — nvim  (35m)\n13:47  Firefox — github.com  (14m)",
        should_speak=False,
    ),
    Scenario(
        "brief social check",
        "15:00  Ghostty — nvim  (40m)\n15:40  Firefox — instagram.com  (4m)",
        should_speak=False,
        plan="finishing the PR",
    ),
    Scenario(
        "lunch",
        "12:00  away from the desk  (55m)",
        should_speak=False,
    ),
    Scenario(
        "just started something new",
        "16:00  Firefox — youtube.com  (6m)",
        should_speak=False,
        note="Six minutes is not yet a problem.",
    ),
    Scenario(
        "finished and said so",
        "17:00  Ghostty — nvim  (90m)\n18:30  Firefox — github.com  (5m)",
        should_speak=False,
        note="No stated plan to drift from.",
    ),
    # --- should speak ---
    Scenario(
        "sustained drift from a stated plan",
        "10:00  Ghostty — nvim  (8m)\n10:08  Firefox — reddit.com  (47m)",
        should_speak=True,
        plan="writing the report, for the next two hours",
    ),
    Scenario(
        "very long unbroken screen time",
        "09:00  Ghostty — nvim  (200m)",
        should_speak=True,
        note="Three and a half hours without a break.",
    ),
    Scenario(
        "middle of the night",
        "02:10  Firefox — youtube.com  (65m)",
        should_speak=True,
        note="Time of day is the whole signal here.",
    ),
    Scenario(
        "plan long overrun",
        "09:00  Firefox — twitter.com  (95m)",
        should_speak=True,
        plan="a quick 20 minute email catch-up",
    ),
    Scenario(
        "phone scrolling at length",
        "20:00  Instagram — 52m",
        should_speak=True,
        plan="going to the gym at eight",
    ),
    Scenario(
        "drift straight after saying otherwise",
        "11:00  Firefox — reddit.com  (35m)",
        should_speak=True,
        plan="not looking at Reddit today",
    ),
]


@dataclass
class Result:
    engine: str
    correct: int = 0
    false_alarms: int = 0
    misses: int = 0
    errors: int = 0
    seconds: float = 0.0
    lines: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.lines = []

    @property
    def total(self) -> int:
        return self.correct + self.false_alarms + self.misses

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


async def run_engine(name: str, config: Config, verbose: bool) -> Result:
    spec = config.models.engines.get(name)
    if spec is None:
        raise SystemExit(f"no engine named {name!r} in config")

    engine = build_engine(name, spec, config.models.max_tokens)
    if not engine.available:
        raise SystemExit(
            f"engine {name!r} is not usable — check its base_url, or set "
            f"{spec.api_key_env or 'its API key'}"
        )

    # A real Agent so the system prompt under test is the actual one, persona
    # and proactivity level included. An eval against a simplified prompt would
    # measure the wrong thing.
    from juno.agent import Agent

    memory = Memory(":memory:")
    agent = Agent(config, memory, devices=None)

    result = Result(engine=f"{name} ({engine.model})")
    started = time.monotonic()

    for scenario in SCENARIOS:
        if scenario.plan:
            memory.remember("current task", scenario.plan, source="eval")
        else:
            memory.forget("current task")

        prompt = CHECK_IN_PROMPT.format(digest=scenario.digest)
        try:
            reply = await engine.complete(agent.system_prompt(), [{"role": "user", "content": prompt}], [])
        except EngineError as exc:
            result.errors += 1
            result.lines.append(f"  ERROR  {scenario.name}: {exc}")
            continue

        silent = is_silence(reply.text)
        spoke = not silent

        if spoke == scenario.should_speak:
            result.correct += 1
            mark = "ok    "
        elif spoke:
            result.false_alarms += 1
            mark = "NAGGED"
        else:
            result.misses += 1
            mark = "missed"

        line = f"  {mark}  {scenario.name}"
        if verbose and spoke:
            line += f"\n           → {reply.text[:160]}"
        result.lines.append(line)

    result.seconds = time.monotonic() - started
    memory.close()
    return result


def report(results: list[Result]) -> None:
    print()
    for result in results:
        print(f"{result.engine}")
        for line in result.lines:
            print(line)
        print()

    width = max(len(r.engine) for r in results)
    print(f"{'engine'.ljust(width)}   correct   false alarms   misses   errors   time")
    print("-" * (width + 52))
    for r in results:
        print(
            f"{r.engine.ljust(width)}   "
            f"{r.correct:>2}/{r.total:<4}   "
            f"{r.false_alarms:>12}   "
            f"{r.misses:>6}   "
            f"{r.errors:>6}   "
            f"{r.seconds:>5.0f}s"
        )
    print()
    print("False alarms are the ones that matter — they are what makes Juno")
    print("intolerable. A model with more misses but no false alarms is safer.")


async def main_async(args: argparse.Namespace) -> int:
    try:
        config = Config.load()
    except ValueError:
        # The eval doesn't need a device token; defaults are enough to exercise
        # the prompt.
        config = Config(auth_token="eval", user_name="Aoi", models=ModelConfig())

    results = []
    for name in args.engine:
        print(f"running {len(SCENARIOS)} scenarios against {name}…", file=sys.stderr)
        results.append(await run_engine(name, config, args.verbose))

    report(results)
    worst = max(results, key=lambda r: r.false_alarms)
    return 1 if worst.false_alarms > len(SCENARIOS) // 4 else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine",
        action="append",
        default=None,
        help="Engine name from config. Repeatable, to compare side by side.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show what it actually said."
    )
    args = parser.parse_args()
    args.engine = args.engine or ["local"]
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
