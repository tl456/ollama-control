"""HTTP route groups, separate from service logic and central access checks.

The server facade is injected rather than imported to avoid circular imports and
to preserve existing diagnostic/test patches on app.server module attributes.
"""
from . import admin, knowledge, memory, upgrade, skills, tool_services, office, interactions


def install_routes(application, context):
    exported = {}
    for group in (admin, memory, knowledge, upgrade, skills, tool_services, office, interactions):
        router = group.build_router(context)
        application.include_router(router)
        for route in router.routes:
            exported[route.endpoint.__name__] = route.endpoint
    return exported
