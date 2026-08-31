import asyncio, uuid, json
from app.database import SessionLocal
from app.services.llm_router import LLMEndpoint
from app.services.synexia.fsm import SynexiaFSM
from app.services.synexia.fsm import ExecutionRequest

async def main():
    db = SessionLocal()
    ep = LLMEndpoint(base_url='http://10.10.10.46:8000/v1', api_key='sk-none', model_id='qwen3.6-27b', provider='Custom')
    kb_id = '59311886-ec3e-49c0-9d86-a2c23e9fb495'
    proj_id = 'a3dc76e3-6d04-4bcf-9c14-55d1a92a4d07'
    req = ExecutionRequest(
        conversation_id=str(uuid.uuid4()),
        agent_name='ecisco_bi_assistant',
        user_message='Provide me last month sales data in doc format',
        user_id='00000000-0000-0000-0000-000000000000',
        mode='dynamic',
        org_id='default-org',
        app_id='default-app',
        data_ctx_extras={
            'bound_kb_ids': [kb_id],
            'project_id': proj_id,
            'resource_type': 'project',
            'resource_id': proj_id,
        },
        endpoint=ep,
    )
    verify_verdicts = []
    plan_nodes = []
    fsm = SynexiaFSM(db)
    result = await asyncio.to_thread(
        fsm.run, req, None, True, None,
        on_verify=lambda passed, r: verify_verdicts.append((passed, [c['check'] for c in r.checks])),
    )
    # inspect plan + observations
    for n in (fsm.plan.nodes or []):
        plan_nodes.append((n.node_type, n.status, (n.error or '')[:80]))
    obs = [(o.observation_type, o.success, (o.error_message or '')[:60]) for o in (fsm.execution.observations or [])]
    db.close()
    print('STATE:', result.state)
    print('VERIFY VERDICTS:', verify_verdicts)
    print('PLAN NODES:', plan_nodes)
    print('OBS:', obs)
    print('CONFIDENCE:', fsm.execution.confidence_score)
    content = (result.assistant_content or '')[:600].replace('\n', ' ')
    print('CONTENT:', content)

asyncio.run(main())
