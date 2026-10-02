"""P0-03 clean-host drill entrypoint.

Needs no secret and writes nothing outside durable state, so it is safe to run
more than once and safe to run on a host other than the one it was released on.
"""

from otter import Context

from logic import next_count

ctx = Context.from_environment()
count = next_count(ctx.state.get("count"))
ctx.state.set("count", count)
ctx.log.info("drill-job ran", count=count)
print("drill-job: run %d" % count)
