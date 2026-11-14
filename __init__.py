"""Native Hermes plugin entrypoint."""
from .provider_ops import post_llm_call

def register(ctx):
    ctx.register_hook('post_llm_call', post_llm_call)
