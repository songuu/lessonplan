import asyncio

from tests.conftest import login


async def wait_document(client, headers, document_id):
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"/api/v1/knowledge-documents/{document_id}", headers=headers)
            item = response.json()["data"]
            if item["status"] in {"ready", "failed"}:
                assert item["status"] == "ready", item
                return item
            await asyncio.sleep(.02)


async def test_independent_knowledge_ingestion_search_and_private_boundaries(client):
    teacher = await login(client)
    other = await login(client, "other-knowledge@example.test")
    response = await client.post("/api/v1/knowledge-bases", headers=teacher,
                                 json={"title": "几何备课资料", "description": "图形定义与课堂活动"})
    assert response.status_code == 200, response.text
    base = response.json()["data"]
    upload = await client.post(f"/api/v1/knowledge-bases/{base['id']}/documents", headers=teacher,
        files={"file": ("长方形.md", "# 长方形\n长方形有四个直角，对边相等。用折纸检验对边。\n正方形四条边相等。".encode(), "text/markdown")})
    assert upload.status_code == 200, upload.text
    document = await wait_document(client, teacher, upload.json()["data"]["id"])
    assert document["chunk_count"] > 0
    chunks = await client.get(f"/api/v1/knowledge-documents/{document['id']}/chunks", headers=teacher)
    assert chunks.json()["data"]["items"][0]["ordinal"] == 0
    result = await client.post(f"/api/v1/knowledge-bases/{base['id']}/search", headers=teacher,
                               json={"query": "长方形对边", "top_k": 3})
    hits = result.json()["data"]
    assert hits["method"] == "lexical_bm25"
    assert hits["items"][0]["filename"] == "长方形.md"
    assert hits["items"][0]["score"] > 0
    empty = await client.post(f"/api/v1/knowledge-bases/{base['id']}/search", headers=teacher,
                              json={"query": "photosynthesis", "top_k": 3})
    assert empty.json()["data"]["items"] == []
    for path in [f"/knowledge-bases/{base['id']}/documents", f"/knowledge-documents/{document['id']}",
                 f"/knowledge-documents/{document['id']}/chunks"]:
        assert (await client.get('/api/v1' + path, headers=other)).status_code == 404
    assert (await client.post(f"/api/v1/knowledge-bases/{base['id']}/search", headers=other,
                              json={"query": "长方形"})).status_code == 404
    assert (await client.get('/api/v1/personal-lessons', headers=teacher)).json()['data'] == []
    assert (await client.patch(f"/api/v1/knowledge-bases/{base['id']}", headers=teacher,
                               json={"title": "修改标题", "description": "更新描述"})).status_code == 200
    assert (await client.delete(f"/api/v1/knowledge-documents/{document['id']}", headers=teacher)).status_code == 200
    assert (await client.get(f"/api/v1/knowledge-documents/{document['id']}", headers=teacher)).status_code == 404


async def test_knowledge_context_is_frozen_in_draft_and_foreign_bases_rejected(client):
    teacher = await login(client)
    foreign = await login(client, "foreign-base@example.test")
    base = (await client.post('/api/v1/knowledge-bases', headers=teacher, json={"title": "分数"})).json()["data"]
    upload = await client.post(f"/api/v1/knowledge-bases/{base['id']}/documents", headers=teacher,
        files={"file": ("定义.txt", "分数表示一个整体被平均分后的若干份。分母表示平均分的份数。".encode(), "text/plain")})
    document = await wait_document(client, teacher, upload.json()["data"]["id"])
    body = {"request_id": "knowledge-freeze", "topic": "分数的初步认识", "school_stage": "primary", "grade": "grade_3",
        "subject": "math", "knowledge_base_ids": [base['id']]}
    made = await client.post('/api/v1/lesson-drafts/generations/one-click', headers=teacher, json=body)
    assert made.status_code == 200, made.text
    draft_id = made.json()['data']['draft_id']
    before = (await client.get('/api/v1/lesson-drafts/' + draft_id, headers=teacher)).json()['data']
    assert before['knowledge_context_snapshot'][0]['document_id'] == document['id']
    assert (await client.post('/api/v1/lesson-drafts/generations/one-click', headers=foreign,
        json={**body, "request_id": "foreign-attempt"})).status_code == 404
    await client.delete(f"/api/v1/knowledge-bases/{base['id']}", headers=teacher)
    after = (await client.get('/api/v1/lesson-drafts/' + draft_id, headers=teacher)).json()['data']
    assert after['knowledge_context_snapshot'] == before['knowledge_context_snapshot']
    again = await client.post('/api/v1/lesson-drafts/generations/one-click', headers=teacher, json=body)
    assert again.json()['data'] == made.json()['data']


async def test_knowledge_upload_authentication_and_parse_failure_are_explicit(client):
    assert (await client.post('/api/v1/knowledge-bases/unknown/documents', files={'file': ('x.txt', b'x')})).status_code == 401
    teacher = await login(client)
    base = (await client.post('/api/v1/knowledge-bases', headers=teacher, json={"title": "教材"})).json()['data']
    bad = await client.post(f"/api/v1/knowledge-bases/{base['id']}/documents", headers=teacher,
        files={'file': ('not-a-pdf.pdf', b'not pdf', 'application/pdf')})
    assert bad.status_code == 200
    doc_id = bad.json()['data']['id']
    async with asyncio.timeout(10):
        while True:
            document = (await client.get('/api/v1/knowledge-documents/' + doc_id, headers=teacher)).json()['data']
            if document['status'] == 'failed':
                break
            await asyncio.sleep(.02)
    assert document['error_message']


async def test_deleted_document_cannot_publish_late_parsing_results(client, monkeypatch):
    import threading

    from sqlalchemy import func, select

    from app.db.models import KnowledgeChunk, Run
    from app.integrations.documents import DocumentAdapter

    entered, release = threading.Event(), threading.Event()
    original = DocumentAdapter.parse_upload

    def delayed_parse(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DocumentAdapter, 'parse_upload', delayed_parse)
    teacher = await login(client)
    base = (await client.post('/api/v1/knowledge-bases', headers=teacher, json={'title': '并发删除'})).json()['data']
    upload = await client.post(f"/api/v1/knowledge-bases/{base['id']}/documents", headers=teacher,
        files={'file': ('notes.txt', '长方形对边相等。'.encode(), 'text/plain')})
    document = upload.json()['data']
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        removed = await client.delete('/api/v1/knowledge-documents/' + document['id'], headers=teacher)
        assert removed.status_code == 200
    finally:
        release.set()
    pending = list(client.app.state.dispatcher.tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    async with client.app.state.db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.document_id == document['id'])) == 0
        assert (await session.get(Run, document['run_id'])).status == 'cancelled'


async def test_upload_failure_can_be_explicitly_reindexed_and_empty_scope_is_respected(client):
    teacher = await login(client)
    base = (await client.post('/api/v1/knowledge-bases', headers=teacher, json={'title': '索引重建'})).json()['data']
    upload = await client.post(f"/api/v1/knowledge-bases/{base['id']}/documents", headers=teacher,
        files={'file': ('notes.txt', '分数分母表示平均分的份数。'.encode(), 'text/plain')})
    document = await wait_document(client, teacher, upload.json()['data']['id'])
    rebuilt = await client.post('/api/v1/knowledge-documents/' + document['id'] + '/reindex', headers=teacher)
    assert rebuilt.status_code == 200
    assert rebuilt.json()['data']['run_id'] != document['run_id']
    await wait_document(client, teacher, document['id'])
    body = {'request_id': 'without-knowledge', 'topic': '分数', 'school_stage': 'primary', 'grade': 'grade_3',
            'subject': 'math', 'knowledge_base_ids': []}
    made = await client.post('/api/v1/lesson-drafts/generations/one-click', headers=teacher, json=body)
    draft = await client.get('/api/v1/lesson-drafts/' + made.json()['data']['draft_id'], headers=teacher)
    assert draft.json()['data']['knowledge_context_snapshot'] == []


async def test_markdown_extension_is_supported_and_cancelled_index_can_be_rebuilt(client):
    teacher = await login(client)
    base = (await client.post('/api/v1/knowledge-bases', headers=teacher, json={'title': 'Markdown资料'})).json()['data']
    original_submit = client.app.state.dispatcher.submit

    async def pause(*args, **kwargs):
        pass

    client.app.state.dispatcher.submit = pause
    upload = await client.post(f"/api/v1/knowledge-bases/{base['id']}/documents", headers=teacher,
        files={'file': ('notes.markdown', '分数表示平均分后的若干份。'.encode(), 'text/markdown')})
    document = upload.json()['data']
    try:
        cancelled = await client.post('/api/v1/generation-runs/' + document['run_id'] + '/cancel', headers=teacher)
        assert cancelled.status_code == 200
        current = (await client.get('/api/v1/knowledge-documents/' + document['id'], headers=teacher)).json()['data']
        assert current['status'] == 'failed' and current['error_message']
    finally:
        client.app.state.dispatcher.submit = original_submit
    rebuilt = await client.post('/api/v1/knowledge-documents/' + document['id'] + '/reindex', headers=teacher)
    assert rebuilt.status_code == 200
    assert (await wait_document(client, teacher, document['id']))['chunk_count'] > 0
