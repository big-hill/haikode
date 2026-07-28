#ifndef HAI_MESSAGES_H
#define HAI_MESSAGES_H

#include <SupportDefs.h>

// Per Codex spec §4.1: four-char constants, namespaced for the app.
// These are the protocol between UI (HaiWindow) and domain (AppController / gateways).

enum : uint32 {
	// Window → Controller
	kMsgSendPrompt   = 'hSnd',  // "text", "sink" (BMessenger to window), "gen" (int32)
	kMsgCancelRun    = 'hCan',  // "gen"
	kMsgNewSession   = 'hNew',  // start a new durable desktop conversation
	kMsgSelectSession = 'hSel', // "name": select an existing durable conversation

	// Controller / gateway → Window (streaming)
	kMsgStreamDelta  = 'hDlt',  // "text", "gen"
	kMsgStreamReasoning = 'hRsn', // "text", "gen": model thinking, rendered dim
	kMsgToolStarted  = 'hTSt',  // "name", "title", "gen"
	kMsgToolResult   = 'hTRs',  // "name", "title", ["diff"], ["output"], "gen"
	// "name", "error", "gen", plus "kind" ("failed"|"denied") and "denied"
	kMsgToolFailed   = 'hTEr',
	kMsgRunStatus    = 'hSts',  // "text", "gen"
	// The agent behind this run: "agent", "provider", "model", "directory",
	// "tools", "window", "gen". Sent once, right after the agent is built.
	kMsgSessionInfo  = 'hInf',
	// The context meter and the token/cost counters: "context" and "summary"
	// are ready to display, "percent"/"used"/"window"/"cost" are the numbers.
	kMsgUsage        = 'hUsg',
	kMsgTodos        = 'hTdo',  // "text" (one todo per line), "summary", "gen"
	kMsgRunCompleted = 'hRCm',  // "gen", ["summary"]
	kMsgRunCancelled = 'hRCn',  // "gen"
	kMsgRunFailed    = 'hREr',  // "error", "gen", ["kind"] from ProviderError
	// "id", "text", "gen", plus optional "title"/"permission"/"command"/"diff"
	kMsgApprovalRequested = 'hARe',
	kMsgApprovalResponse  = 'hARs', // "id", "response": once|always|reject

	// App / other → Controller
	kMsgOpenProject  = 'hOpn',  // "path"

	// Internal UI messages (window local)
	kMsgUiSend       = 'hUSn',
	kMsgUiStop       = 'hUSt',
};

#endif
