"""
The clean-room core: keeping each managed address on each lock as wanted.

Built from the problem, not from the integration's earlier core, and kept in
small pieces that are each tested on their own. The pure ones (values,
observations, belief, breakers, refusals, judge) import neither Home
Assistant nor asyncio, and take the time they need as an argument.
"""
