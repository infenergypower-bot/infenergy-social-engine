"""Authenticated Production Desk API, mounted under the existing OS auth boundary."""
from __future__ import annotations

from production_workflow import Conflict, Production


def handle(method, path, payload, data_dir):
    store=Production(data_dir)
    stem="/api/os/production"
    route=path[len(stem):].strip('/').split('/')
    if method=="GET" and route==["state"]:return 200,store.snapshot()
    if method=="POST" and route==["connections","verify"]:
        from production_connections import verify
        result=verify()
        with store.db() as c:
            for provider,detail in result.items():store.event(c,None,"CONNECTION_CHECK",{"provider":provider,**detail})
        return 200,{"connections":result}

    if method=="POST" and route==["packages"]:return 201,{"package":store.create(payload)}
    if method=="POST" and route==["references"]:return 201,{"reference_id":store.register_reference(payload)}
    if method=="POST" and route==["assets"]:return 201,{"asset":store.upload(payload)}
    if method=="POST" and route==["import"]:return 201,{"package":store.import_outbox(payload["outbox_id"],payload["format"])}
    if method=="POST" and route==["examples"]:
        store.add_example(payload);return 201,{"status":"saved"}
    if method=="POST" and len(route)==3 and route[0]=="jobs" and route[2]=="retry":
        return 202,store.retry_job(route[1],acknowledge_possible_spend=payload.get("acknowledge_possible_spend") is True)
    if len(route)>=2 and route[0]=="packages":
        identifier=route[1]
        if method=="GET" and len(route)==2:return 200,{"package":store.get(identifier),"checks":store.checks(identifier)}
        if method=="PATCH" and len(route)==2:
            updates=dict(payload.get("updates",{}))
            if "assets" in updates:
                retained={a["sha256"]:a for a in store.get(identifier)["payload"]["assets"]}
                records=[store.delivery_asset(s) for s in updates["assets"]]
                updates["assets"]=[{**retained.get(a["sha256"],{}),**a} for a in records]
            return 200,{"package":store.revise(identifier,updates,int(payload["revision"]))}
        if method=="POST" and route[2:]==["approve"]:return 200,{"package":store.approve(identifier,int(payload["revision"]))}
        if method=="POST" and route[2:]==["queue"]:return 202,store.queue(identifier,str(payload["stage"]),int(payload["revision"]))
        if method=="POST" and route[2:]==["clone"]:return 201,{"package":store.clone(identifier)}
        if method=="POST" and route[2:]==["estimate"]:return 200,store.estimate(identifier,payload)
        if method=="POST" and route[2:]==["observations"]:
            store.observe(identifier,payload);return 201,{"status":"recorded"}
    return 404,{"error":"production_route_not_found"}
