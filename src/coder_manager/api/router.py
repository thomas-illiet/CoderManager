"""Top-level API router."""

from fastapi import APIRouter

from coder_manager.api.routes import (
    databases,
    health,
    instances,
    jobs,
    members,
    template_images,
    templates,
    workspaces,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(databases.router)
api_router.include_router(instances.router)
api_router.include_router(jobs.router)
api_router.include_router(members.router)
api_router.include_router(templates.router)
api_router.include_router(template_images.router)
api_router.include_router(workspaces.router)
