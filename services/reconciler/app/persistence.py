from __future__ import annotations
import os
from typing import Any

class EvidenceStore:
    """Optional persistence adapters. Reconciliation remains deterministic if external stores are unavailable."""

    def __init__(self) -> None:
        self.mongo_url = os.getenv("MONGO_URL")
        self.elasticsearch_url = os.getenv("ELASTICSEARCH_URL")
        self.postgres_dsn = os.getenv("POSTGRES_DSN")

    async def archive_raw_event(self, event: dict[str, Any]) -> None:
        if not self.mongo_url:
            return
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(self.mongo_url)
        try:
            await client.pickuppact.raw_events.update_one(
                {"event_id": event["event_id"]},
                {"$setOnInsert": event},
                upsert=True,
            )
        finally:
            client.close()

    async def index_incident(self, incident: dict[str, Any]) -> None:
        if not self.elasticsearch_url:
            return
        from elasticsearch import AsyncElasticsearch
        es = AsyncElasticsearch(self.elasticsearch_url)
        try:
            await es.index(index="pickup-pact-incidents", document=incident)
        finally:
            await es.close()

    async def append_audit(self, aggregate_id: str, result: dict[str, Any]) -> None:
        if not self.postgres_dsn:
            return
        import asyncpg, json
        conn = await asyncpg.connect(self.postgres_dsn)
        try:
            await conn.execute(
                """insert into reconciliation_audit(aggregate_id, result_json)
                   values($1, $2::jsonb)""",
                aggregate_id,
                json.dumps(result, default=str),
            )
        finally:
            await conn.close()
