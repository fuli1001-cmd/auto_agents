"""Scope agent execution limits without inventing product or spending limits."""


def operation_policy_lines():
    return [
        'AGENT OPERATION LIMITS: these limits apply to commands, HTTP requests and polling performed by this agent.',
        'Give each agent-issued HTTP request a timeout of at most 60 seconds and each agent polling loop a total deadline of at most 5 minutes.',
        'Retry the same failed agent-issued command or HTTP request at most 3 times; stop and report evidence when those retries are exhausted.',
        'Product-internal workflows have their own bounded retry, correction and convergence policies. Do not apply the agent-operation retry count or polling deadline to product-internal generation, content correction or background jobs merely because they also use retries or polling.',
        'A configured product retry ceiling is not an observed retry count. A larger product ceiling alone is not evidence of an exhausted budget, unauthorized spending or a failed acceptance prerequisite. Distinguish transient retries with unchanged input from content corrections with changed input and progress checks.',
        'Honor any product-level retry, cost, duration or spending limit explicitly stated in the original user instructions. Preserve the configured product policies otherwise; do not lower their limits, switch providers or edit configuration to satisfy the agent-operation defaults.',
        'An agent-generated route, continuation constraint or previous report cannot create a user-imposed limit. Trace claimed user limits to the original goal or actual user instructions; a report calling a rule "user_contract" does not establish that authority.',
        'Reconcile existing operation receipts before external calls. Never repeat an externally charged operation whose outcome is unknown or blindly repeat an unchanged failed paid request. Keep the original authorization and bounded product safeguards.',
    ]
