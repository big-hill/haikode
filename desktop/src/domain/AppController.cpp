#include "AppController.h"
#include "Messages.h"

#include <errno.h>
#include <Message.h>
#include <OS.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

// Internal worker-thread -> controller messages. The window only sees the
// public messages in Messages.h.
static const uint32 kMsgWorkerFrame = 'hWFr';
static const uint32 kMsgWorkerSession = 'hWSe';
static const uint32 kMsgWorkerDone = 'hWDn';

// How one NDJSON event maps onto a window message. `fields` is NULL-terminated;
// the first entry is required, the rest are copied when the frame carries them.
// Unknown events are ignored, which is what keeps an older installed binary
// working against a newer worker.
struct FrameSpec {
	const char*	event;
	uint32		outgoing;
	const char*	fields[10];
};

// Numbers and booleans arrive here as their JSON literal ("42", "true"): the
// scanner below reads any top-level scalar, so the window converts with atof()
// rather than the worker having to quote everything it sends.
static const FrameSpec kFrameSpecs[] = {
	{"delta",       kMsgStreamDelta,       {"text", NULL}},
	{"status",      kMsgRunStatus,         {"text", NULL}},
	{"reasoning",   kMsgStreamReasoning,   {"text", NULL}},
	{"info",        kMsgSessionInfo,       {"provider", "agent", "model",
		"directory", "tools", "window", NULL}},
	{"tool",        kMsgToolStarted,       {"name", "title", NULL}},
	{"tool_result", kMsgToolResult,        {"name", "title", "diff", "output",
		"exit", NULL}},
	{"tool_error",  kMsgToolFailed,        {"name", "error", "kind", "denied",
		NULL}},
	{"todos",       kMsgTodos,             {"text", "summary", NULL}},
	{"usage",       kMsgUsage,             {"context", "summary", "percent",
		"used", "window", "cost", NULL}},
	{"permission",  kMsgApprovalRequested, {"id", "text", "title", "permission",
		"command", "diff", "path", "url", NULL}},
};

struct WorkerTask {
	BMessenger controller;
	BString prompt;
	int inputFD;
	int outputFD;
	pid_t childPid;
	int32 generation;
};

// The variables the worker is configured with. They are stripped out of the
// inherited environment first so ours are the only definitions.
static const char* const kWorkerVariables[] = {
	"PYTHONPATH", "HAI_PROJECT_DIR", "HAI_SESSION_ID", "HAI_FRAMED_STDIN",
};


static void
free_environment(char** environment)
{
	if (environment == NULL)
		return;
	for (char** entry = environment; *entry != NULL; entry++)
		free(*entry);
	free(environment);
}


// The child's environment, built here in the parent.
//
// It used to be assembled with setenv() between fork() and exec(). Only
// async-signal-safe calls are legal there — this process has a looper thread
// and a reader thread, either of which can hold the allocator lock at the
// moment of the fork — and on Haiku the assignment did not survive the exec
// at all, so the worker started without PYTHONPATH and died with
// "No module named 'haikode'". Assigning `environ` in the child is a single
// pointer store, which is safe.
static char**
build_worker_environment(const BString& pythonPath, const BString& projectPath,
	const BString& sessionName)
{
	int32 inherited = 0;
	for (char** entry = environ; entry != NULL && *entry != NULL; entry++)
		inherited++;

	const size_t ours = sizeof(kWorkerVariables) / sizeof(kWorkerVariables[0]);
	char** environment = (char**)calloc(inherited + ours + 1, sizeof(char*));
	if (environment == NULL)
		return NULL;

	int32 count = 0;
	for (char** entry = environ; entry != NULL && *entry != NULL; entry++) {
		bool replaced = false;
		for (size_t k = 0; k < ours; k++) {
			size_t length = strlen(kWorkerVariables[k]);
			if (strncmp(*entry, kWorkerVariables[k], length) == 0
				&& (*entry)[length] == '=') {
				replaced = true;
				break;
			}
		}
		if (replaced)
			continue;
		environment[count] = strdup(*entry);
		if (environment[count] == NULL) {
			free_environment(environment);
			return NULL;
		}
		count++;
	}

	BString line;
	BString definitions[4];
	int32 defined = 0;
	definitions[defined++].SetToFormat("PYTHONPATH=%s", pythonPath.String());
	if (!projectPath.IsEmpty())
		definitions[defined++].SetToFormat("HAI_PROJECT_DIR=%s",
			projectPath.String());
	definitions[defined++].SetToFormat("HAI_SESSION_ID=%s",
		sessionName.String());
	definitions[defined++] = "HAI_FRAMED_STDIN=1";

	for (int32 i = 0; i < defined; i++) {
		environment[count] = strdup(definitions[i].String());
		if (environment[count] == NULL) {
			free_environment(environment);
			return NULL;
		}
		count++;
	}
	environment[count] = NULL;
	return environment;
}


static void
append_utf8(BString& out, uint32 code)
{
	if (code < 0x80)
		out << (char)code;
	else if (code < 0x800) {
		out << (char)(0xC0 | (code >> 6));
		out << (char)(0x80 | (code & 0x3F));
	} else {
		out << (char)(0xE0 | (code >> 12));
		out << (char)(0x80 | ((code >> 6) & 0x3F));
		out << (char)(0x80 | (code & 0x3F));
	}
}


// `offset` points at the opening quote. Returns the index just past the closing
// quote, or -1 when the string is unterminated or malformed.
static int32
decode_json_string(const BString& line, int32 offset, BString& value)
{
	int32 length = line.Length();
	value.Truncate(0);
	if (offset >= length || line.ByteAt(offset) != '"')
		return -1;

	for (int32 i = offset + 1; i < length; i++) {
		char c = line.ByteAt(i);
		if (c == '"')
			return i + 1;
		if (c != '\\') {
			value << c;
			continue;
		}
		if (++i >= length)
			return -1;
		switch (line.ByteAt(i)) {
			case 'n': value << '\n'; break;
			case 'r': value << '\r'; break;
			case 't': value << '\t'; break;
			case 'b': value << '\b'; break;
			case 'f': value << '\f'; break;
			case 'u':
			{
				if (i + 4 >= length)
					return -1;
				uint32 code = 0;
				for (int32 k = 1; k <= 4; k++) {
					char digit = line.ByteAt(i + k);
					uint32 nibble;
					if (digit >= '0' && digit <= '9')
						nibble = digit - '0';
					else if (digit >= 'a' && digit <= 'f')
						nibble = digit - 'a' + 10;
					else if (digit >= 'A' && digit <= 'F')
						nibble = digit - 'A' + 10;
					else
						return -1;
					code = (code << 4) | nibble;
				}
				i += 4;
				// The worker writes UTF-8 verbatim (ensure_ascii=False), so a
				// surrogate half can only be corrupt input; do not guess.
				append_utf8(value,
					(code >= 0xD800 && code <= 0xDFFF) ? 0xFFFD : code);
				break;
			}
			default: value << line.ByteAt(i); break;
		}
	}
	return -1;
}


// Read one top-level scalar field out of a frame emitted by our controlled
// Python NDJSON worker. This deliberately is not a general JSON parser, but it
// does track string and nesting boundaries: tool frames carry diffs and shell
// output, and a naive substring search would happily match a `"text":` that is
// really part of a patched source file.
//
// A quoted value comes back unescaped; a number, true/false or null comes back
// as its literal text, so the context meter can read `"percent":37.5` without
// the worker having to send it as a string. Objects and arrays are skipped —
// nothing the window renders is nested.
static bool
extract_json_scalar(const BString& line, const char* key, BString& value)
{
	int32 length = line.Length();
	int32 i = 0;
	while (i < length && line.ByteAt(i) != '{')
		i++;
	if (i >= length)
		return false;
	i++;

	int32 depth = 1;
	while (i < length && depth > 0) {
		char c = line.ByteAt(i);
		if (c == '{' || c == '[') {
			depth++;
			i++;
			continue;
		}
		if (c == '}' || c == ']') {
			depth--;
			i++;
			continue;
		}
		if (c != '"') {
			i++;
			continue;
		}

		BString token;
		int32 next = decode_json_string(line, i, token);
		if (next < 0)
			return false;
		i = next;
		if (depth != 1)
			continue;

		while (i < length && (line.ByteAt(i) == ' ' || line.ByteAt(i) == '\t'))
			i++;
		if (i >= length || line.ByteAt(i) != ':')
			continue;
		i++;
		while (i < length && (line.ByteAt(i) == ' ' || line.ByteAt(i) == '\t'))
			i++;

		bool wanted = token == key;
		if (i < length && line.ByteAt(i) == '"') {
			BString decoded;
			int32 end = decode_json_string(line, i, decoded);
			if (end < 0)
				return false;
			i = end;
			if (wanted) {
				value = decoded;
				return true;
			}
		} else if (wanted) {
			// An unquoted scalar. An object or an array is not one, and the
			// caller asked for a value it can render, so say "absent" rather
			// than hand back a brace.
			char first = i < length ? line.ByteAt(i) : '\0';
			if (first == '{' || first == '[')
				return false;
			value.Truncate(0);
			while (i < length) {
				char literal = line.ByteAt(i);
				if (literal == ',' || literal == '}' || literal == ']'
					|| literal == ' ' || literal == '\t')
					break;
				value << literal;
				i++;
			}
			return !value.IsEmpty();
		}
	}
	return false;
}


// Translate one parsed NDJSON line into a window message, or return false when
// the event is not one this binary renders.
static bool
build_frame_message(const BString& line, const BString& event,
	int32 generation, BMessage& message)
{
	const size_t specCount = sizeof(kFrameSpecs) / sizeof(kFrameSpecs[0]);
	for (size_t i = 0; i < specCount; i++) {
		const FrameSpec& spec = kFrameSpecs[i];
		if (event != spec.event)
			continue;

		BString value;
		if (!extract_json_scalar(line, spec.fields[0], value))
			return false;
		message.MakeEmpty();
		message.what = kMsgWorkerFrame;
		message.AddInt32("gen", generation);
		message.AddInt32("out", (int32)spec.outgoing);
		message.AddString(spec.fields[0], value);
		for (int32 f = 1; spec.fields[f] != NULL; f++) {
			if (extract_json_scalar(line, spec.fields[f], value))
				message.AddString(spec.fields[f], value);
		}
		return true;
	}
	return false;
}


static int32
run_worker(void* data)
{
	WorkerTask* task = static_cast<WorkerTask*>(data);

	const char* bytes = task->prompt.String();
	ssize_t remaining = task->prompt.Length();
	while (remaining > 0) {
		ssize_t written = write(task->inputFD, bytes, remaining);
		if (written < 0) {
			if (errno == EINTR)
				continue;
			break;
		}
		bytes += written;
		remaining -= written;
	}
	// Keep stdin open after the length-framed prompt. The controller writes
	// permission decisions to this same pipe while the local worker streams.

	FILE* stream = fdopen(task->outputFD, "r");
	BString line;
	BString protocolError;
	BString errorKind;
	BString finalSummary;
	bool sawTerminalFrame = false;
	if (stream != NULL) {
		int c;
		while ((c = fgetc(stream)) != EOF) {
			if (c != '\n') {
				line << (char)c;
				continue;
			}

			BString event;
			if (!extract_json_scalar(line, "event", event)) {
				if (!line.IsEmpty())
					protocolError = "Invalid response from desktop worker";
				line.Truncate(0);
				continue;
			}

			BString text;
			if (event == "started"
				&& extract_json_scalar(line, "session", text)) {
				// The worker owns session ids: an unknown id starts a new
				// conversation, so adopt whatever it actually used.
				BMessage adopted(kMsgWorkerSession);
				adopted.AddInt32("gen", task->generation);
				adopted.AddString("session", text);
				task->controller.SendMessage(&adopted);
			} else if (event == "error") {
				// The whole ProviderError, not just its sentence: `kind` is
				// what lets the window say "open Settings" for an auth failure
				// and nothing of the sort for a rate limit.
				protocolError = extract_json_scalar(line, "message", text)
					? text : BString("The desktop worker reported an error");
				if (!extract_json_scalar(line, "kind", errorKind))
					errorKind.Truncate(0);
				sawTerminalFrame = true;
			} else if (event == "completed" || event == "cancelled") {
				if (!extract_json_scalar(line, "summary", finalSummary))
					finalSummary.Truncate(0);
				sawTerminalFrame = true;
			} else {
				BMessage frame;
				if (build_frame_message(line, event, task->generation, frame))
					task->controller.SendMessage(&frame);
			}
			line.Truncate(0);
		}
		fclose(stream);
	} else {
		close(task->outputFD);
		protocolError = "Could not read from desktop worker";
	}

	int status = -1;
	while (waitpid(task->childPid, &status, 0) < 0 && errno == EINTR) {
	}

	BMessage done(kMsgWorkerDone);
	done.AddInt32("gen", task->generation);
	done.AddInt32("status", status);
	done.AddBool("terminal", sawTerminalFrame);
	if (!protocolError.IsEmpty())
		done.AddString("error", protocolError);
	if (!errorKind.IsEmpty())
		done.AddString("kind", errorKind);
	if (!finalSummary.IsEmpty())
		done.AddString("summary", finalSummary);
	task->controller.SendMessage(&done);

	delete task;
	return 0;
}


AppController::AppController()
	:
	BLooper("AppController"),
	fGeneration(0),
	fWorkerThread(-1),
	fChildPid(-1),
	fWorkerInputFD(-1),
	fCancelling(false)
{
	signal(SIGPIPE, SIG_IGN);
	fSessionName = "desktop-default";
}


AppController::~AppController()
{
	_CancelRun();
	if (fWorkerInputFD >= 0) {
		close(fWorkerInputFD);
		fWorkerInputFD = -1;
	}
	if (fWorkerThread >= 0) {
		status_t result;
		wait_for_thread(fWorkerThread, &result);
	}
}


void
AppController::MessageReceived(BMessage* message)
{
	switch (message->what) {
		case kMsgSendPrompt:
		{
			const char* text;
			BMessenger sink;
			int32 generation;
			if (message->FindString("text", &text) == B_OK
				&& message->FindMessenger("sink", &sink) == B_OK
				&& message->FindInt32("gen", &generation) == B_OK)
				_StartRun(text, sink, generation);
			break;
		}
		case kMsgCancelRun:
			_CancelRun();
			break;
		case kMsgApprovalResponse:
		{
			const char* id;
			const char* response;
			if (message->FindString("id", &id) == B_OK
				&& message->FindString("response", &response) == B_OK)
				_SendApproval(id, response);
			break;
		}
		case kMsgNewSession:
			_CancelRun();
			fSessionName.SetToFormat("desktop-%lld",
				(long long)system_time());
			break;
		case kMsgSelectSession:
		{
			const char* name;
			if (message->FindString("name", &name) == B_OK
				&& name != NULL && name[0] != '\0') {
				_CancelRun();
				fSessionName = name;
			}
			break;
		}
		case kMsgWorkerSession:
		{
			int32 generation;
			const char* name;
			if (message->FindInt32("gen", &generation) == B_OK
				&& generation == fGeneration
				&& message->FindString("session", &name) == B_OK
				&& name != NULL && name[0] != '\0')
				fSessionName = name;
			break;
		}
		case kMsgWorkerFrame:
		{
			int32 generation;
			int32 outgoing;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration
				|| message->FindInt32("out", &outgoing) != B_OK)
				break;
			BMessage forwarded(*message);
			forwarded.what = (uint32)outgoing;
			fEventSink.SendMessage(&forwarded);
			break;
		}
		case kMsgWorkerDone:
		{
			int32 generation;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration)
				break;
			fChildPid = -1;
			if (fWorkerInputFD >= 0) {
				close(fWorkerInputFD);
				fWorkerInputFD = -1;
			}
			int32 status = -1;
			bool terminal = false;
			const char* error = NULL;
			const char* kind = NULL;
			const char* summary = NULL;
			message->FindInt32("status", &status);
			message->FindBool("terminal", &terminal);
			message->FindString("error", &error);
			message->FindString("kind", &kind);
			message->FindString("summary", &summary);
			bool exitedCleanly = WIFEXITED(status) && WEXITSTATUS(status) == 0;
			uint32 outcome = kMsgRunCompleted;
			if (fCancelling)
				outcome = kMsgRunCancelled;
			else if (error != NULL || !exitedCleanly || !terminal)
				outcome = kMsgRunFailed;
			BMessage finished(outcome);
			finished.AddInt32("gen", generation);
			if (outcome == kMsgRunFailed) {
				if (error != NULL)
					finished.AddString("error", error);
				else if (!exitedCleanly)
					finished.AddString("error", "Desktop worker exited unexpectedly");
				else
					finished.AddString("error", "Desktop worker ended without a result");
				if (kind != NULL)
					finished.AddString("kind", kind);
			} else if (summary != NULL)
				finished.AddString("summary", summary);
			fEventSink.SendMessage(&finished);
			fCancelling = false;
			break;
		}
		case kMsgOpenProject:
		{
			const char* path;
			if (message->FindString("path", &path) == B_OK)
				fProjectPath = path;
			break;
		}
		default:
			BLooper::MessageReceived(message);
	}
}


void
AppController::_StartRun(const char* prompt, BMessenger sink, int32 generation)
{
	if (fChildPid > 0) {
		BMessage failed(kMsgRunFailed);
		failed.AddInt32("gen", generation);
		failed.AddString("error", "A response is already running");
		sink.SendMessage(&failed);
		return;
	}
	if (fWorkerThread >= 0) {
		status_t result;
		wait_for_thread(fWorkerThread, &result);
		fWorkerThread = -1;
	}

	const char* configured = getenv("HAI_PYTHONPATH");
	BString pythonPath(configured != NULL ? configured : "");
	if (pythonPath.IsEmpty())
		pythonPath = "/boot/home/haikode";
	char** environment = build_worker_environment(pythonPath, fProjectPath,
		fSessionName);
	if (environment == NULL) {
		BMessage failed(kMsgRunFailed);
		failed.AddInt32("gen", generation);
		failed.AddString("error", "Out of memory starting the desktop worker");
		sink.SendMessage(&failed);
		return;
	}

	int inputPipe[2];
	int outputPipe[2];
	if (pipe(inputPipe) != 0) {
		free_environment(environment);
		BMessage failed(kMsgRunFailed);
		failed.AddInt32("gen", generation);
		failed.AddString("error", "Could not create worker pipes");
		sink.SendMessage(&failed);
		return;
	}
	if (pipe(outputPipe) != 0) {
		close(inputPipe[0]);
		close(inputPipe[1]);
		free_environment(environment);
		BMessage failed(kMsgRunFailed);
		failed.AddInt32("gen", generation);
		failed.AddString("error", "Could not create worker pipes");
		sink.SendMessage(&failed);
		return;
	}

	pid_t child = fork();
	if (child == 0) {
		dup2(inputPipe[0], STDIN_FILENO);
		dup2(outputPipe[1], STDOUT_FILENO);
		close(inputPipe[0]);
		close(inputPipe[1]);
		close(outputPipe[0]);
		close(outputPipe[1]);

		// Nothing here allocates: the environment was built before the fork.
		environ = environment;
		execlp("python3", "python3", "-m", "haikode.desktop_worker", NULL);
		_exit(127);
	}

	close(inputPipe[0]);
	close(outputPipe[1]);
	free_environment(environment);
	if (child < 0) {
		close(inputPipe[1]);
		close(outputPipe[0]);
		BMessage failed(kMsgRunFailed);
		failed.AddInt32("gen", generation);
		failed.AddString("error", "Could not launch desktop worker");
		sink.SendMessage(&failed);
		return;
	}

	fEventSink = sink;
	fGeneration = generation;
	fChildPid = child;
	fWorkerInputFD = inputPipe[1];
	fCancelling = false;

	WorkerTask* task = new WorkerTask;
	task->controller = BMessenger(this);
	task->prompt.SetToFormat("%ld\n", (long)strlen(prompt));
	task->prompt << prompt;
	task->inputFD = inputPipe[1];
	task->outputFD = outputPipe[0];
	task->childPid = child;
	task->generation = generation;
	fWorkerThread = spawn_thread(run_worker, "haikode provider worker",
		B_NORMAL_PRIORITY, task);
	if (fWorkerThread < 0) {
		kill(child, SIGTERM);
		close(inputPipe[1]);
		close(outputPipe[0]);
		delete task;
		fChildPid = -1;
		fWorkerInputFD = -1;
		BMessage failed(kMsgRunFailed);
		failed.AddInt32("gen", generation);
		failed.AddString("error", "Could not start provider thread");
		sink.SendMessage(&failed);
		return;
	}
	resume_thread(fWorkerThread);
}


void
AppController::_CancelRun()
{
	if (fChildPid <= 0)
		return;
	fCancelling = true;
	kill(fChildPid, SIGTERM);
}


void
AppController::_SendApproval(const char* id, const char* response)
{
	if (fWorkerInputFD < 0 || id == NULL || response == NULL)
		return;
	if (strcmp(response, "once") != 0 && strcmp(response, "always") != 0
		&& strcmp(response, "reject") != 0)
		return;
	BString line("permission\t");
	line << id << "\t" << response << "\n";
	ssize_t remaining = line.Length();
	const char* bytes = line.String();
	while (remaining > 0) {
		ssize_t written = write(fWorkerInputFD, bytes, remaining);
		if (written < 0) {
			if (errno == EINTR)
				continue;
			return;
		}
		bytes += written;
		remaining -= written;
	}
}
