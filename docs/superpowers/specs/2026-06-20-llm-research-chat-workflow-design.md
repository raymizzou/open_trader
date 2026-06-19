# LLM Research Chat Workflow Design

## Goal

Add a dashboard workflow where the user can discuss a holding with an LLM using
the latest TradingAgents research bundle and local user context, then explicitly
generate a standardized final conclusion that the dashboard can render.

The first version should make the chat workflow useful without turning the
dashboard into an automated trading decision engine. TradingAgents' original
research conclusion is shown immediately. The user/LLM conclusion is missing
until the user clicks `生成最终结论` and the backend writes a validated conclusion
artifact.

## User Experience

In the symbol detail panel, show a `投研结论` area with two cards:

- `投研给出的结论`: shown as soon as a TradingAgents research bundle exists for
  the symbol.
- `我和 LLM 探讨后的结论`: shown as `缺失` until a finalized conclusion exists.

Add a `开始讨论` or `继续讨论` button near these cards. When clicked:

- The dashboard opens a chat panel for the current holding.
- The user does not copy or paste system prompts.
- The backend creates or resumes a chat session with the latest TradingAgents
  context and local user context already loaded.
- The user can ask multiple follow-up questions in normal chat form.
- Intermediate chat replies are saved in the session but do not update the
  dashboard conclusion card.

Add a `生成最终结论` button inside the chat panel. When clicked:

- The backend sends the original context plus the chat transcript to the LLM.
- The LLM must return a strict `user.llm_conclusion.v1` JSON object.
- The backend validates the JSON.
- On success, the backend writes `user_llm_conclusion.json`, refreshes or
  rewrites the local `dashboard_view.json`, and the dashboard card updates.
- On failure, the chat panel shows a clear error and keeps the old conclusion.

## Data Flow

```text
TradingAgents export
  -> research_data/<ticker>/<date>/dashboard_view.json
  -> research_data/<ticker>/<date>/combined_input.json
  -> research_data/<ticker>/<date>/llm_system_prompt.md

open_trader dashboard load
  -> reads latest dashboard_view.json by market/symbol
  -> attaches research_view to matching holding
  -> frontend shows TradingAgents conclusion immediately

user opens chat
  -> POST /api/research-chat/sessions
  -> backend loads llm_system_prompt.md and combined_input.json
  -> backend creates session transcript under data/research_chat/sessions/

multi-turn chat
  -> POST /api/research-chat/sessions/<id>/messages
  -> backend calls configured LLM
  -> backend appends user/assistant messages

generate final conclusion
  -> POST /api/research-chat/sessions/<id>/finalize
  -> backend calls LLM with context + transcript + strict JSON instruction
  -> backend validates user.llm_conclusion.v1
  -> backend writes user_llm_conclusion.json and dashboard_view.json
  -> frontend refreshes research_view
```

## Backend Design

Add a small backend module for research chat orchestration. It should not live
inside TradingAgents integration code, because it owns user interaction state
and dashboard update behavior.

Responsibilities:

- Locate the latest research bundle for a holding.
- Load `dashboard_view.json`, `combined_input.json`, and `llm_system_prompt.md`.
- Create and persist a chat session.
- Append chat messages.
- Call the configured LLM for chat replies.
- Call the configured LLM again for `生成最终结论`.
- Validate and write `user_llm_conclusion.json`.
- Refresh the corresponding `dashboard_view.json`.

Suggested session shape:

```json
{
  "schema_version": "open_trader.research_chat_session.v1",
  "session_id": "20260620T103000-US-NVDA",
  "market": "US",
  "symbol": "NVDA",
  "research_bundle_dir": "research_data/NVDA/2026-06-19",
  "status": "active",
  "created_at": "2026-06-20T10:30:00+08:00",
  "updated_at": "2026-06-20T10:35:00+08:00",
  "messages": [
    {"role": "user", "content": "请先解释这个结论最脆弱的假设。"},
    {"role": "assistant", "content": "最脆弱的假设是收入增速持续高于市场预期。"}
  ]
}
```

Suggested final conclusion schema:

```json
{
  "schema_version": "user.llm_conclusion.v1",
  "status": "present",
  "content": "我确认后的结论：暂不加仓，等待财报后重新评估。",
  "updated_at": "2026-06-20T10:40:00+08:00",
  "source": "downstream_llm_conversation",
  "conversation_reference": "data/research_chat/sessions/20260620T103000-US-NVDA.json"
}
```

## API Design

Add local dashboard APIs:

```text
POST /api/research-chat/sessions
GET  /api/research-chat/sessions/<session_id>
POST /api/research-chat/sessions/<session_id>/messages
POST /api/research-chat/sessions/<session_id>/finalize
```

`POST /api/research-chat/sessions` input:

```json
{
  "market": "US",
  "symbol": "NVDA"
}
```

Response:

```json
{
  "session_id": "20260620T103000-US-NVDA",
  "market": "US",
  "symbol": "NVDA",
  "research_view": {"schema_version": "dashboard.research_view.v1"},
  "messages": []
}
```

`POST /messages` input:

```json
{
  "content": "请解释为什么 TradingAgents 给出 Overweight。"
}
```

Response includes the appended assistant message and current transcript.

`POST /finalize` response:

```json
{
  "status": "ok",
  "conclusion": {"schema_version": "user.llm_conclusion.v1"},
  "dashboard_view": {"schema_version": "dashboard.research_view.v1"}
}
```

## Frontend Design

The frontend stays as static HTML/CSS/JavaScript.

Symbol detail view changes:

- Add a `投研结论` section with two cards.
- Add a `开始讨论` or `继续讨论` button.
- Show `缺失` for the user/LLM conclusion until a finalized conclusion exists.

Chat panel:

- Opens over or beside the symbol detail panel.
- Shows the selected market/symbol and latest research date.
- Shows the transcript.
- Provides a message input and send button.
- Provides a `生成最终结论` button.
- Shows a loading state while chat or finalization calls are running.
- Keeps the finalization button disabled while there is no assistant/user
  discussion content.

Rendering rules:

- Never update `我和 LLM 探讨后的结论` from intermediate chat messages.
- Only update that card from validated `dashboard_view.json`.
- If finalization fails, keep the previous card state.
- Long conclusions should wrap, not overflow card bounds.
- The UI remains Chinese-facing.

## Error Handling

- Missing research bundle: show `暂无投研上下文，无法开始讨论`.
- Missing `llm_system_prompt.md` or `combined_input.json`: fail session creation
  with a clear backend error.
- LLM call failure during chat: append no assistant message and show retryable
  error.
- LLM finalization returns invalid JSON: show `最终结论格式无效，请重试`.
- Valid JSON with `status != "present"` or empty `content`: reject it and keep
  the existing dashboard state.
- File write failure: show an error and do not update frontend state.

## Security And Safety Boundaries

- This workflow does not place orders.
- It does not update trading plans or trade actions.
- It does not infer the user's final conclusion automatically.
- It requires the user to click `生成最终结论` before writing conclusion data.
- It should store only local session transcripts and JSON artifacts.
- It should not log API keys or raw provider credentials.

## Testing Strategy

Backend tests:

- Loading a holding with a TradingAgents bundle attaches `research_view`.
- Missing bundle attaches both cards as `缺失`.
- Creating a chat session loads context automatically.
- Sending a message appends user and assistant messages.
- Finalization writes `user_llm_conclusion.json` and updates
  `dashboard_view.json`.
- Invalid finalization JSON is rejected and does not update files.

Frontend tests:

- Static shell contains the research conclusion cards, chat entry button, chat
  panel, message input, and `生成最终结论` button.
- JavaScript renders exactly two conclusion cards.
- Missing user/LLM conclusion displays `缺失`.
- Intermediate chat messages do not update the final conclusion card.
- Finalize success refreshes the final conclusion card from returned
  `dashboard_view`.

Manual browser verification:

- Start the local dashboard.
- Open a symbol with a sample research bundle.
- Confirm the TradingAgents conclusion is visible immediately.
- Open chat and confirm the first request does not require copying context.
- Send a message.
- Click `生成最终结论`.
- Confirm the final conclusion card updates and missing text disappears.
