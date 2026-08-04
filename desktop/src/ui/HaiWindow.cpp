#include "HaiWindow.h"
#include "SettingsWindow.h"
#include "../domain/Messages.h"
#include "../domain/ConfigBridge.h"

#include <Alert.h>
#include <Application.h>
#include <Button.h>
#include <Directory.h>
#include <Entry.h>
#include <FilePanel.h>
#include <File.h>
#include <Font.h>
#include <GraphicsDefs.h>
#include <GroupView.h>
#include <InterfaceDefs.h>
#include <LayoutBuilder.h>
#include <ListItem.h>
#include <ListView.h>
#include <Menu.h>
#include <MenuBar.h>
#include <MenuField.h>
#include <MenuItem.h>
#include <PopUpMenu.h>
#include <OS.h>
#include <OutlineListView.h>
#include <Path.h>
#include <ScrollView.h>
#include <SplitView.h>
#include <StatusBar.h>
#include <String.h>
#include <StringList.h>
#include <StringView.h>
#include <TabView.h>
#include <TextView.h>
#include <UTF8.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// M0 — 100% native Haiku BeAPI UI
// - BMenuBar, BSplitView, BOutlineListView (Tracker-like), BTextView, BStatusBar
// - ui_color() + be_*_font everywhere
// - Controller owns a cancellable Python provider worker — never blocks the window looper
// This combination is instantly recognizable as a classic Haiku app.

enum : uint32 {
	MSG_NEW_SESSION     = 'NSES',
	MSG_OPEN_PROJECT    = 'OPRO',
	MSG_ATTACH_FILE     = 'ATFL',
	MSG_FILES_SELECTED  = 'FSEL',
	MSG_CLEAR           = 'CLER',
	MSG_PROJECT_INVOKED = 'PINV',
	MSG_SESSION_INVOKED = 'SINV',
	MSG_RELOAD_SESSIONS = 'SRLD',
	MSG_SESSIONS_LOADED = 'SLDD',
	MSG_HISTORY_LOADED  = 'HLDD',
	MSG_AGENT_PROVIDER_LOADED = 'APrv',
	MSG_AGENT_MODEL_LOADED    = 'AMdl',
	MSG_TOOL_SELECTED   = 'TSLD',
	MSG_APPROVE_ONCE    = 'AONC',
	MSG_APPROVE_ALWAYS  = 'AALW',
	MSG_DENY_TOOL       = 'ADNY',
	MSG_ABOUT           = 'ABOU',
	MSG_PROVIDERS_LISTED = 'PLst',
	MSG_PROVIDER_PICKED  = 'PPck',
	MSG_PROVIDER_SET     = 'PSet',
	MSG_SHOW_SETTINGS   = 'STNG',
};


class SessionItem : public BStringItem {
public:
	SessionItem(const char* name, const char* title)
		:
		BStringItem(title != NULL && title[0] != '\0' ? title : name),
		fName(name != NULL ? name : "")
	{
	}

	const BString& Name() const { return fName; }

private:
	BString fName;
};


class ComposerTextView : public BTextView {
public:
	ComposerTextView(const char* name, BMessenger target)
		:
		BTextView(name),
		fTarget(target)
	{
		SetWordWrap(true);
		SetStylable(false);
	}

	virtual void KeyDown(const char* bytes, int32 numBytes)
	{
		if (numBytes == 1 && (bytes[0] == '\n' || bytes[0] == '\r')
			&& (modifiers() & B_COMMAND_KEY) != 0) {
			fTarget.SendMessage(kMsgUiSend);
			return;
		}
		BTextView::KeyDown(bytes, numBytes);
	}

private:
	BMessenger fTarget;
};


class ApprovalItem : public BStringItem {
public:
	ApprovalItem(const char* id, const char* text)
		:
		BStringItem(text != NULL ? text : "Agent permission"),
		fID(id != NULL ? id : "")
	{
	}

	const BString& ID() const { return fID; }

private:
	BString fID;
};


struct HistoryTask {
	BMessenger target;
	BString args;
	uint32 replyWhat;
};


static int32
run_history_task(void* data)
{
	HistoryTask* task = static_cast<HistoryTask*>(data);
	int exitCode = -1;
	BString output = ConfigBridge::RunConfigTool(task->args, &exitCode);
	BMessage result(task->replyWhat);
	result.AddInt32("exit", exitCode);
	result.AddString("output", output);
	task->target.SendMessage(&result);
	delete task;
	return 0;
}


static void
spawn_history_task(BMessenger target, const BString& args, uint32 replyWhat)
{
	HistoryTask* task = new HistoryTask;
	task->target = target;
	task->args = args;
	task->replyWhat = replyWhat;
	thread_id thread = spawn_thread(run_history_task, "haikode history loader",
		B_NORMAL_PRIORITY, task);
	if (thread >= 0)
		resume_thread(thread);
	else
		delete task;
}


static void
populate_project_tree(BOutlineListView* list, BStringItem* parent,
	const BPath& directoryPath, int32 depth, int32& count)
{
	if (depth < 0 || count >= 500)
		return;
	BDirectory directory(directoryPath.Path());
	if (directory.InitCheck() != B_OK)
		return;

	BEntry entry;
	while (count < 500 && directory.GetNextEntry(&entry, false) == B_OK) {
		char name[B_FILE_NAME_LENGTH];
		if (entry.GetName(name) != B_OK || name[0] == '\0')
			continue;
		BStringItem* item = new BStringItem(name);
		list->AddUnder(item, parent);
		count++;
		if (entry.IsDirectory() && depth > 0) {
			BPath childPath;
			if (entry.GetPath(&childPath) == B_OK)
				populate_project_tree(list, item, childPath, depth - 1, count);
		}
	}
}

HaiWindow::HaiWindow(BRect frame, BMessenger controller)
	:
	BWindow(frame, "haikode", B_TITLED_WINDOW,
		B_QUIT_ON_WINDOW_CLOSE | B_AUTO_UPDATE_SIZE_LIMITS),
	fController(controller),
	fRunning(false),
	fGeneration(0)
{
	// (BWindow has no SetViewColor; child views use ui_color-based
	// defaults for the standard panel background.)

	// === Native MenuBar (classic Haiku) ===
	BMenuBar* menuBar = new BMenuBar("menubar");

	BMenu* fileMenu = new BMenu("File");
	fileMenu->AddItem(new BMenuItem("New Session", new BMessage(MSG_NEW_SESSION), 'N'));
	fileMenu->AddItem(new BMenuItem("Open Project...", new BMessage(MSG_OPEN_PROJECT), 'O'));
	fileMenu->AddItem(new BMenuItem("Attach File" B_UTF8_ELLIPSIS,
		new BMessage(MSG_ATTACH_FILE), 'L'));
	fileMenu->AddSeparatorItem();
	// ',' is the standard Haiku shortcut for settings/preferences
	fileMenu->AddItem(new BMenuItem("Settings" B_UTF8_ELLIPSIS,
		new BMessage(MSG_SHOW_SETTINGS), ','));
	fileMenu->AddSeparatorItem();
	fileMenu->AddItem(new BMenuItem("Quit", new BMessage(B_QUIT_REQUESTED), 'Q'));
	menuBar->AddItem(fileMenu);

	BMenu* editMenu = new BMenu("Edit");
	editMenu->AddItem(new BMenuItem("Clear Transcript", new BMessage(MSG_CLEAR)));
	menuBar->AddItem(editMenu);

	BMenu* viewMenu = new BMenu("View");
	viewMenu->AddItem(new BMenuItem("Clear Transcript", new BMessage(MSG_CLEAR)));
	viewMenu->AddSeparatorItem();
	viewMenu->AddItem(new BMenuItem("Project Sidebar", new BMessage('VSID')));  // stub for M1 toggle
	menuBar->AddItem(viewMenu);

	BMenu* sessionMenu = new BMenu("Session");
	sessionMenu->AddItem(new BMenuItem("Compact Context", new BMessage('CMPC')));
	sessionMenu->AddItem(new BMenuItem("Show Cost", new BMessage('COST')));
	menuBar->AddItem(sessionMenu);

	// Switching provider lives one click away, not behind Settings: the
	// menu lists every configured profile with its model, marks the one a
	// run would use, and keeps Settings for keys and endpoints.
	fProviderMenu = new BMenu("Provider");
	fProviderMenu->SetRadioMode(true);
	menuBar->AddItem(fProviderMenu);

	BMenu* helpMenu = new BMenu("Help");
	helpMenu->AddItem(new BMenuItem("About haikode", new BMessage(MSG_ABOUT)));
	menuBar->AddItem(helpMenu);

	// === Left: ChatGPT-style sessions + native project tree tabs ===
	fSessionList = new BListView("sessions", B_SINGLE_SELECTION_LIST);
	fSessionList->SetViewColor(ui_color(B_DOCUMENT_BACKGROUND_COLOR));
	fSessionList->SetFont(be_plain_font);
	fSessionList->SetInvocationMessage(new BMessage(MSG_SESSION_INVOKED));
	BScrollView* sessionsScroll = new BScrollView("ss", fSessionList,
		0, false, true);
	BButton* reloadSessions = new BButton("reloadSessions", "Reload",
		new BMessage(MSG_RELOAD_SESSIONS));
	BGroupView* sessionsPane = new BGroupView(B_VERTICAL);
	BLayoutBuilder::Group<>(sessionsPane, B_VERTICAL, 3)
		.SetInsets(4, 4, 4, 4)
		.Add(sessionsScroll)
		.Add(reloadSessions)
	.End();

	// Project tree — classic native BOutlineListView (Tracker style)
	fProjectList = new BOutlineListView("project", B_SINGLE_SELECTION_LIST);
	fProjectList->SetViewColor(ui_color(B_DOCUMENT_BACKGROUND_COLOR));
	fProjectList->SetFont(be_plain_font);
	fProjectList->SetInvocationMessage(new BMessage(MSG_PROJECT_INVOKED));

	BScrollView* projectScroll = new BScrollView("ps", fProjectList, 0, false, true);

	BStringView* projectLabel = new BStringView("pl", "Project");
	projectLabel->SetFont(be_bold_font);

	BGroupView* projectPane = new BGroupView(B_VERTICAL);
	BLayoutBuilder::Group<>(projectPane, B_VERTICAL, 3)
		.SetInsets(6, 6, 6, 6)
		.Add(projectLabel)
		.Add(projectScroll)
	.End();

	BTabView* leftPane = new BTabView("leftTabs", B_WIDTH_FROM_WIDEST);
	// AddTab() resets a BTab's label from the view it adopts, so labels set
	// before it silently vanish — the field report was "nameless tabs, an
	// enigma". Label AFTER adding.
	BTab* sessionsTab = new BTab();
	leftPane->AddTab(sessionsPane, sessionsTab);
	sessionsTab->SetLabel("Sessions");
	BTab* projectTab = new BTab();
	leftPane->AddTab(projectPane, projectTab);
	projectTab->SetLabel("Project");

	fProjectList->AddItem(new BStringItem("No project selected"));

	// === Center: Transcript (native BTextView) + composer ===
	fTranscript = new BTextView("transcript");   // layout constructor (no stale BRect)
	fTranscript->MakeEditable(false);
	fTranscript->MakeSelectable(true);
	fTranscript->SetViewColor(ui_color(B_DOCUMENT_BACKGROUND_COLOR));
	fTranscript->SetFont(be_plain_font);
	fTranscript->SetWordWrap(true);
	fTranscript->SetStylable(true);  // allow native styled runs for "You:" / "haikode:" labels (very Haiku)

	BScrollView* transcriptScroll = new BScrollView("sc", fTranscript, 0, false, true);

	fInput = new ComposerTextView("input", BMessenger(this));
	fInput->SetViewColor(ui_color(B_DOCUMENT_BACKGROUND_COLOR));
	fInput->SetFont(be_plain_font);
	float composerHeight = be_plain_font->Size() * 4.5f;
	fInput->SetExplicitMinSize(BSize(B_SIZE_UNSET, composerHeight));
	BScrollView* inputScroll = new BScrollView("inputScroll", fInput,
		0, false, true);

	BButton* sendBtn = new BButton("send", "Send", new BMessage(kMsgUiSend));
	BButton* stopBtn = new BButton("stop", "Stop", new BMessage(kMsgUiStop));

	// The model picker lives beside the input, where every current chat
	// application puts it (Claude, Cursor: the composer corner) — and a
	// label-less BMenuField showing the marked item is the native way to
	// say it. The menu bar's Provider menu stays for discoverability.
	fModelPopup = new BPopUpMenu("model");
	BMenuField* modelField = new BMenuField("modelField", NULL, fModelPopup);
	modelField->SetExplicitMaxSize(
		BSize(be_plain_font->StringWidth("M") * 18, B_SIZE_UNSET));

	BView* composer = new BView("composer", B_WILL_DRAW);
	BLayoutBuilder::Group<>(composer, B_HORIZONTAL, 4)
		.SetInsets(6, 4, 6, 6)
		.Add(inputScroll)
		.Add(modelField)
		.Add(sendBtn)
		.Add(stopBtn)
	.End();

	// Header strip: which agent/model is answering on the left, how full the
	// context window is on the right — the two things opencode keeps beside
	// its prompt. Both are filled in from worker frames; until the first run
	// they say so rather than showing a stale zero.
	fInfoView = new BStringView("mi",
		"No agent yet  •  choose a provider and model in Settings");
	fInfoView->SetFont(be_plain_font);
	fInfoView->SetHighUIColor(B_PANEL_TEXT_COLOR);
	fInfoView->SetExplicitMaxSize(BSize(B_SIZE_UNLIMITED, B_SIZE_UNSET));

	fContextBar = new BStatusBar("context", "Context");
	fContextBar->SetMaxValue(100.0f);
	fContextBar->SetBarColor(ui_color(B_STATUS_BAR_COLOR));
	fContextBar->SetTrailingText("idle");
	fContextBar->SetFont(be_plain_font);
	// Derived from the system font rather than a pixel constant, so the meter
	// keeps its proportions at any font size (HIG).
	fContextBar->SetExplicitMaxSize(
		BSize(be_plain_font->StringWidth("M") * 20, B_SIZE_UNSET));

	BView* infoStrip = new BView("info", B_WILL_DRAW);
	BLayoutBuilder::Group<>(infoStrip, B_HORIZONTAL, B_USE_SMALL_SPACING)
		.SetInsets(8, 2, 8, 2)
		.Add(fInfoView)
		.AddGlue()
		.Add(fContextBar)
	.End();

	BGroupView* centerPane = new BGroupView(B_VERTICAL);
	BLayoutBuilder::Group<>(centerPane, B_VERTICAL, 0)
		.Add(infoStrip)
		.Add(transcriptScroll)
		.Add(composer)
	.End();

	// === Right: Tools & Approvals — another native BOutlineListView ===
	fToolList = new BOutlineListView("tools", B_SINGLE_SELECTION_LIST);
	fToolList->SetViewColor(ui_color(B_DOCUMENT_BACKGROUND_COLOR));
	fToolList->SetFont(be_plain_font);
	fToolList->SetSelectionMessage(new BMessage(MSG_TOOL_SELECTED));

	BScrollView* toolScroll = new BScrollView("ts", fToolList, 0, false, true);

	BStringView* toolLabel = new BStringView("tl", "Tools & Approvals");
	toolLabel->SetFont(be_bold_font);

	fApproveOnceButton = new BButton("approveOnce", "Once",
		new BMessage(MSG_APPROVE_ONCE));
	fApproveAlwaysButton = new BButton("approveAlways", "Always",
		new BMessage(MSG_APPROVE_ALWAYS));
	fDenyButton = new BButton("deny", "Deny", new BMessage(MSG_DENY_TOOL));
	fApproveOnceButton->SetEnabled(false);
	fApproveAlwaysButton->SetEnabled(false);
	fDenyButton->SetEnabled(false);

	BGroupView* rightPane = new BGroupView(B_VERTICAL);
	BLayoutBuilder::Group<>(rightPane, B_VERTICAL, 3)
		.SetInsets(6, 6, 6, 6)
		.Add(toolLabel)
		.Add(toolScroll)
		.AddGroup(B_HORIZONTAL, B_USE_SMALL_SPACING)
			.Add(fDenyButton)
			.AddGlue()
			.Add(fApproveOnceButton)
			.Add(fApproveAlwaysButton)
		.End()
	.End();

	// Tool/approval surface for permission events from the local worker.
	fApprovalRoot = new BStringItem("Pending Approvals");
	fToolList->AddItem(fApprovalRoot);
	// The plan the model published with todowrite. It sits above the log
	// because it says what is *going* to happen, which is the part a user
	// watches.
	fTodoRoot = new BStringItem("Plan");
	fToolList->AddItem(fTodoRoot);
	fToolLogRoot = new BStringItem("Tool Log");
	fToolList->AddItem(fToolLogRoot);

	// === Main native resizable split (BSplitView is classic Haiku) ===
	BSplitView* mainSplit = new BSplitView(B_HORIZONTAL, B_USE_DEFAULT_SPACING);
	mainSplit->SetName("mainSplit");
	mainSplit->AddChild(leftPane);
	mainSplit->AddChild(centerPane);
	mainSplit->AddChild(rightPane);

	// === Native BStatusBar ===
	// No label: BStatusBar paints label and text side by side, which glued
	// "Ready" onto every later status ("ReadyConnecting to ...").
	fStatusBar = new BStatusBar("status", "");
	fStatusBar->SetBarColor(ui_color(B_STATUS_BAR_COLOR));
	fStatusBar->SetText("Ready");

	// Root layout
	BLayoutBuilder::Group<>(this, B_VERTICAL, 0)
		.Add(menuBar)
		.Add(mainSplit)
		.Add(fStatusBar)
	.End();

	// The transcript starts empty; what the app is and how it works lives
	// in Help > About, not in promotional boilerplate above the first reply
	// (field request).

	fInput->MakeFocus(true);
	SetSizeLimits(720, 2400, 520, 1600);
	spawn_history_task(BMessenger(this), "sessions", MSG_SESSIONS_LOADED);
	// Whether a run would have an agent right now — the header claimed
	// "No agent yet" forever, even with a provider configured and signed
	// in, because nothing ever asked.
	spawn_history_task(BMessenger(this), "get default_provider",
		MSG_AGENT_PROVIDER_LOADED);
	spawn_history_task(BMessenger(this), "list-providers",
		MSG_PROVIDERS_LISTED);
}

void
HaiWindow::MessageReceived(BMessage* message)
{
	switch (message->what) {
		case kMsgUiSend:
		{
			if (fRunning) {
				// Never a silent no-op: the silence read as a dead app.
				fStatusBar->SetText(
					"A reply is still running" B_UTF8_ELLIPSIS
					"  press Stop first");
				break;
			}
			const char* text = fInput->Text();
			if (text == NULL || text[0] == '\0') break;

			// Styled labels using native BTextView runs — feels like classic Haiku text views
			_AppendStyledLabel("\nYou: ", true);
			_Append(text);
			_Append("\n");
			_AppendStyledLabel("haikode: ", false);

			fGeneration++;
			BMessage prompt(kMsgSendPrompt);
			BString request(text);
			if (!fPendingAttachments.IsEmpty()) {
				request << "\n\nThe user explicitly attached these files as context:"
					<< fPendingAttachments;
			}
			prompt.AddString("text", request);
			prompt.AddMessenger("sink", BMessenger(this));
			prompt.AddInt32("gen", fGeneration);
			fController.SendMessage(&prompt);

			fInput->SetText("");
			fPendingAttachments.Truncate(0);
			_SetRunning(true);
			fStatusBar->SetText("Connecting to configured provider...");
			break;
		}

		case kMsgUiStop:
			if (fRunning)
				fController.SendMessage(kMsgCancelRun);
			break;

		case kMsgConfigChanged:
			// Settings saved: ask again which agent a run would use.
			spawn_history_task(BMessenger(this), "get default_provider",
				MSG_AGENT_PROVIDER_LOADED);
			spawn_history_task(BMessenger(this), "list-providers",
				MSG_PROVIDERS_LISTED);
			break;

		case MSG_PROVIDERS_LISTED:
		{
			if (message->GetInt32("exit", -1) != 0)
				break;
			while (fProviderMenu->CountItems() > 0)
				delete fProviderMenu->RemoveItem((int32)0);
			while (fModelPopup->CountItems() > 0)
				delete fModelPopup->RemoveItem((int32)0);
			BStringList lines;
			BString(message->GetString("output", "")).Split("\n", true,
				lines);
			for (int32 i = 0; i < lines.CountStrings(); i++) {
				BStringList fields;
				lines.StringAt(i).Split("\t", false, fields);
				if (fields.CountStrings() < 1
					|| fields.StringAt(0).IsEmpty())
					continue;
				BString name = fields.StringAt(0);
				BString label(name);
				if (fields.CountStrings() >= 4
					&& !fields.StringAt(3).IsEmpty())
					label << " \xe2\x80\x94 " << fields.StringAt(3);
				BMessage* pick = new BMessage(MSG_PROVIDER_PICKED);
				pick->AddString("name", name);
				BMenuItem* item = new BMenuItem(label, pick);
				if (name == fAgentProvider)
					item->SetMarked(true);
				fProviderMenu->AddItem(item);
				BMessage* pickToo = new BMessage(*pick);
				BMenuItem* popupItem = new BMenuItem(label, pickToo);
				if (name == fAgentProvider)
					popupItem->SetMarked(true);
				fModelPopup->AddItem(popupItem);
			}
			fProviderMenu->AddSeparatorItem();
			fProviderMenu->AddItem(new BMenuItem(
				"Model & keys" B_UTF8_ELLIPSIS,
				new BMessage(MSG_SHOW_SETTINGS)));
			break;
		}

		case MSG_PROVIDER_PICKED:
		{
			const char* name = message->GetString("name", NULL);
			if (name == NULL || name[0] == '\0')
				break;
			BString args("set default_provider ");
			args << ConfigBridge::ShellQuote(name);
			spawn_history_task(BMessenger(this), args, MSG_PROVIDER_SET);
			break;
		}

		case MSG_PROVIDER_SET:
			// Re-read what a run would use now; rebuild the checkmarks.
			spawn_history_task(BMessenger(this), "get default_provider",
				MSG_AGENT_PROVIDER_LOADED);
			spawn_history_task(BMessenger(this), "list-providers",
				MSG_PROVIDERS_LISTED);
			break;

		case MSG_AGENT_PROVIDER_LOADED:
		{
			BString provider = message->GetString("output", "");
			provider.Trim();
			if (message->GetInt32("exit", -1) != 0 || provider.IsEmpty())
				break;      // the choose-in-Settings line stays honest
			fAgentProvider = provider;
			BString args("get providers.");
			args << provider << ".model";
			spawn_history_task(BMessenger(this), args,
				MSG_AGENT_MODEL_LOADED);
			break;
		}

		case MSG_AGENT_MODEL_LOADED:
		{
			BString info(fAgentProvider);
			BString model = message->GetString("output", "");
			model.Trim();
			if (message->GetInt32("exit", -1) == 0 && !model.IsEmpty())
				info << " / " << model;
			info << "  •  ready — the first Send starts the agent";
			fInfoView->SetText(info.String());
			break;
		}

		case kMsgStreamDelta:
		{
			int32 generation;
			const char* text;
			if (message->FindInt32("gen", &generation) == B_OK
				&& generation == fGeneration
				&& message->FindString("text", &text) == B_OK)
			{
				_Append(text);
			}
			break;
		}

		case kMsgStreamReasoning:
		{
			int32 generation;
			const char* text;
			if (message->FindInt32("gen", &generation) == B_OK
				&& generation == fGeneration
				&& message->FindString("text", &text) == B_OK)
			{
				_AppendStyled(text, _DimTextColor(), false);
				_Append("\n");
			}
			break;
		}

		case kMsgToolStarted:
		{
			int32 generation;
			const char* name;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration
				|| message->FindString("name", &name) != B_OK)
				break;
			BString line("\n· ");
			line << name;
			const char* title = message->GetString("title", NULL);
			if (title != NULL && title[0] != '\0' && strcmp(title, name) != 0)
				line << "  " << title;
			line << "\n";
			_AppendStyled(line.String(), _DimTextColor(), true);
			_LogToolActivity(line.String());
			BString status("Running ");
			status << name << B_UTF8_ELLIPSIS;
			fStatusBar->SetText(status.String());
			break;
		}

		case kMsgToolResult:
		{
			int32 generation;
			const char* name;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration
				|| message->FindString("name", &name) != B_OK)
				break;
			const char* diff = message->GetString("diff", NULL);
			if (diff != NULL && diff[0] != '\0')
				_AppendDiff(diff);
			BString entry("  ");
			entry << name << ": " << message->GetString("title", "done");
			_LogToolActivity(entry.String());
			break;
		}

		case kMsgToolFailed:
		{
			int32 generation;
			const char* name;
			const char* error;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration
				|| message->FindString("name", &name) != B_OK
				|| message->FindString("error", &error) != B_OK)
				break;
			// A tool the user declined is not a malfunction, and saying so in
			// the same red sentence as a crash trains people to ignore both.
			bool denied = strcmp(message->GetString("kind", "failed"),
				"denied") == 0;
			BString line("\n[");
			line << name << "] " << (denied ? "declined: " : "") << error
				<< "\n";
			_AppendStyled(line.String(),
				denied ? _DimTextColor() : ui_color(B_FAILURE_COLOR), false);
			_LogToolActivity(line.String());
			break;
		}

		case kMsgSessionInfo:
		{
			int32 generation;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration)
				break;
			BString info(message->GetString("agent", ""));
			if (info.IsEmpty())
				info = "build";
			info << " agent  •  " << message->GetString("provider", "?");
			const char* model = message->GetString("model", "");
			if (model[0] != '\0')
				info << "/" << model;
			const char* directory = message->GetString("directory", "");
			if (directory[0] != '\0')
				info << "  •  " << directory;
			fInfoView->SetText(info.String());

			BString tip("Tools: ");
			tip << message->GetString("tools", "(none)");
			const char* window = message->GetString("window", "");
			if (window[0] != '\0')
				tip << "\nContext window: " << window << " tokens";
			fInfoView->SetToolTip(tip.String());
			break;
		}

		case kMsgUsage:
		{
			int32 generation;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration)
				break;
			_SetContext(atof(message->GetString("percent", "0")),
				message->GetString("context", ""),
				message->GetString("summary", ""));
			break;
		}

		case kMsgTodos:
		{
			int32 generation;
			const char* text;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration
				|| message->FindString("text", &text) != B_OK)
				break;
			_SetTodos(text, message->GetString("summary", ""));
			break;
		}

		case kMsgRunStatus:
		{
			int32 generation;
			const char* text;
			if (message->FindInt32("gen", &generation) == B_OK
				&& generation == fGeneration
				&& message->FindString("text", &text) == B_OK) {
				fStatusBar->SetText(text);
				BString activity(text);
				if (activity.StartsWith("[tool]")) {
					fToolList->AddUnder(new BStringItem(text), fToolLogRoot);
					fToolList->Expand(fToolLogRoot);
				}
			}
			break;
		}

		case kMsgRunFailed:
		{
			// A run ending — any run, any generation — means nothing is
			// running now. The generation gates only the transcript side
			// effects; letting a stale end-frame keep fRunning true made
			// every later Send die on its guard in total silence.
			_SetRunning(false);
			int32 generation;
			const char* error;
			if (message->FindInt32("gen", &generation) == B_OK
				&& generation == fGeneration
				&& message->FindString("error", &error) == B_OK) {
				// The provider's own classification, so the advice underneath
				// fits the failure: only an auth problem is fixed in Settings.
				BString kind(message->GetString("kind", ""));
				BString line("\n[Error");
				if (!kind.IsEmpty() && kind != "unknown")
					line << ": " << kind;
				line << "] " << error << "\n";
				_AppendStyled(line.String(), ui_color(B_FAILURE_COLOR), true);
				if (kind == "auth")
					fStatusBar->SetText(
						"Not signed in — File ▸ Settings ▸ Sign in");
				else if (kind == "rate_limit")
					fStatusBar->SetText("Rate limited — try again shortly");
				else if (kind == "context_overflow")
					fStatusBar->SetText(
						"Context window full — start a new session");
				else if (kind == "model_not_found")
					fStatusBar->SetText("Unknown model — check Settings");
				else
					fStatusBar->SetText("Provider error — check Settings");
				_SetRunning(false);
				_ClearPendingApprovals();
			}
			break;
		}

		case kMsgApprovalRequested:
		{
			int32 generation;
			const char* id;
			const char* text;
			if (message->FindInt32("gen", &generation) != B_OK
				|| generation != fGeneration
				|| message->FindString("id", &id) != B_OK
				|| message->FindString("text", &text) != B_OK)
				break;
			// Newer workers send a full sentence in "title"; "text" is the
			// original terse label and stays the fallback.
			const char* title = message->GetString("title", NULL);
			const char* label = (title != NULL && title[0] != '\0') ? title : text;
			ApprovalItem* item = new ApprovalItem(id, label);
			fToolList->AddUnder(item, fApprovalRoot);
			fToolList->Expand(fApprovalRoot);
			fToolList->Select(fToolList->IndexOf(item));
			fApproveOnceButton->SetEnabled(true);
			fApproveAlwaysButton->SetEnabled(true);
			fDenyButton->SetEnabled(true);

			// Approving blind is not a real choice, so the transcript shows
			// exactly what is being asked for before the buttons light up.
			BString request("\n[Permission] ");
			request << label << "\n";
			_AppendStyled(request.String(), ui_color(B_FAILURE_COLOR), true);
			const char* command = message->GetString("command", NULL);
			if (command != NULL && command[0] != '\0') {
				BString shell("  $ ");
				shell << command << "\n";
				_AppendStyled(shell.String(), _DimTextColor(), false,
					be_fixed_font);
			}
			const char* diff = message->GetString("diff", NULL);
			if (diff != NULL && diff[0] != '\0')
				_AppendDiff(diff);
			fStatusBar->SetText("The agent is waiting for tool approval");
			break;
		}

		case MSG_TOOL_SELECTED:
		{
			int32 index = fToolList->CurrentSelection();
			BListItem* item = index >= 0 ? fToolList->ItemAt(index) : NULL;
			bool approval = item != NULL
				&& fToolList->Superitem(item) == fApprovalRoot;
			fApproveOnceButton->SetEnabled(approval);
			fApproveAlwaysButton->SetEnabled(approval);
			fDenyButton->SetEnabled(approval);
			break;
		}

		case MSG_APPROVE_ONCE:
		case MSG_APPROVE_ALWAYS:
		case MSG_DENY_TOOL:
		{
			int32 index = fToolList->CurrentSelection();
			BListItem* raw = index >= 0 ? fToolList->ItemAt(index) : NULL;
			if (raw == NULL || fToolList->Superitem(raw) != fApprovalRoot)
				break;
			ApprovalItem* item = static_cast<ApprovalItem*>(raw);
			const char* response = message->what == MSG_APPROVE_ONCE ? "once"
				: (message->what == MSG_APPROVE_ALWAYS ? "always" : "reject");
			BMessage decision(kMsgApprovalResponse);
			decision.AddString("id", item->ID());
			decision.AddString("response", response);
			fController.SendMessage(&decision);
			fToolList->RemoveItem(item);
			delete item;
			fApproveOnceButton->SetEnabled(false);
			fApproveAlwaysButton->SetEnabled(false);
			fDenyButton->SetEnabled(false);
			fStatusBar->SetText("Permission response sent");
			break;
		}

		case kMsgRunCompleted:
		case kMsgRunCancelled:
		{
			_SetRunning(false);      // ends always end; see kMsgRunFailed
			int32 generation;
			if (message->FindInt32("gen", &generation) != B_OK || generation != fGeneration)
				break;

			if (message->what == kMsgRunCancelled)
				_Append(" [stopped]\n");
			_Append("\n");

			_SetRunning(false);
			_ClearPendingApprovals();
			// The worker's own one-line usage summary when it sent one; it
			// already knows how to format tokens and money.
			const char* summary = message->GetString("summary", "");
			fStatusBar->SetText(summary[0] != '\0' ? summary : "Ready");
			spawn_history_task(BMessenger(this), "sessions",
				MSG_SESSIONS_LOADED);
			break;
		}

		case MSG_RELOAD_SESSIONS:
			spawn_history_task(BMessenger(this), "sessions",
				MSG_SESSIONS_LOADED);
			break;

		case MSG_SESSIONS_LOADED:
		{
			if (message->GetInt32("exit", -1) != 0)
				break;
			while (fSessionList->CountItems() > 0)
				delete fSessionList->RemoveItem((int32)0);
			BString output = message->GetString("output", "");
			BStringList lines;
			output.Split("\n", true, lines);
			for (int32 i = 0; i < lines.CountStrings(); i++) {
				BStringList fields;
				lines.StringAt(i).Split("\t", false, fields);
				if (fields.CountStrings() < 1 || fields.StringAt(0).IsEmpty())
					continue;
				BString title = fields.CountStrings() > 1
					? fields.StringAt(1) : fields.StringAt(0);
				fSessionList->AddItem(new SessionItem(
					fields.StringAt(0).String(), title.String()));
			}
			break;
		}

		case MSG_SESSION_INVOKED:
		{
			int32 index = fSessionList->CurrentSelection();
			SessionItem* item = index >= 0
				? static_cast<SessionItem*>(fSessionList->ItemAt(index)) : NULL;
			if (item == NULL)
				break;
			BMessage select(kMsgSelectSession);
			select.AddString("name", item->Name());
			fController.SendMessage(&select);
			// The plan and the meter belong to the conversation that was on
			// screen; the next run refills them for this one.
			_ResetMeters();
			BString args("session-text ");
			args << ConfigBridge::ShellQuote(item->Name());
			spawn_history_task(BMessenger(this), args, MSG_HISTORY_LOADED);
			BString title("haikode — ");
			title << item->Text();
			SetTitle(title.String());
			fStatusBar->SetText("Loading conversation...");
			break;
		}

		case MSG_HISTORY_LOADED:
		{
			if (message->GetInt32("exit", -1) != 0) {
				fStatusBar->SetText("Could not load conversation");
				break;
			}
			BString output = message->GetString("output", "");
			fTranscript->SetText(output.String());
			fTranscript->ScrollToOffset(fTranscript->TextLength());
			fStatusBar->SetText("Conversation loaded");
			break;
		}

		case MSG_NEW_SESSION:
			fController.SendMessage(kMsgNewSession);
			fTranscript->SetText("");
			SetTitle("haikode");
			_ResetMeters();
			_Append("New durable conversation.\n\n");
			fStatusBar->SetText("New session • Ready");
			break;

		case MSG_OPEN_PROJECT:
			{
				// Real native Haiku BFilePanel — instantly recognizable
				BFilePanel* panel = new BFilePanel(
					B_OPEN_PANEL,
					new BMessenger(this),
					NULL,
					B_DIRECTORY_NODE,   // directories only (project root)
					false,              // multiple selection off
					NULL, NULL, false, true);
				panel->SetButtonLabel(B_DEFAULT_BUTTON, "Open Project");
				panel->Show();
			}
			break;

		case MSG_ATTACH_FILE:
		{
			BFilePanel* panel = new BFilePanel(
				B_OPEN_PANEL, new BMessenger(this), NULL, B_FILE_NODE,
				true, new BMessage(MSG_FILES_SELECTED), NULL, false, true);
			panel->SetButtonLabel(B_DEFAULT_BUTTON, "Attach");
			panel->Show();
			break;
		}

		case MSG_FILES_SELECTED:
		{
			const off_t kMaxFileBytes = 256 * 1024;
			const int32 kMaxAttachmentBytes = 512 * 1024;
			entry_ref ref;
			int32 attached = 0;
			for (int32 i = 0; message->FindRef("refs", i, &ref) == B_OK; i++) {
				BEntry entry(&ref, true);
				BPath path;
				if (entry.GetPath(&path) != B_OK)
					continue;
				BFile file(&entry, B_READ_ONLY);
				off_t size = 0;
				if (file.InitCheck() != B_OK || file.GetSize(&size) != B_OK
					|| size < 0 || size > kMaxFileBytes)
					continue;
				if (fPendingAttachments.Length() + size > kMaxAttachmentBytes)
					break;

				char* buffer = new char[(size_t)size + 1];
				ssize_t bytes = file.Read(buffer, (size_t)size);
				if (bytes < 0) {
					delete[] buffer;
					continue;
				}
				bool binary = false;
				for (ssize_t j = 0; j < bytes; j++) {
					if (buffer[j] == '\0') {
						binary = true;
						break;
					}
				}
				if (!binary) {
					fPendingAttachments << "\n\n--- BEGIN FILE: "
						<< path.Path() << " ---\n";
					fPendingAttachments.Append(buffer, (int32)bytes);
					fPendingAttachments << "\n--- END FILE ---";
					attached++;
				}
				delete[] buffer;
			}

			BString status;
			if (attached > 0)
				status.SetToFormat("%ld file(s) attached to the next message",
					(long)attached);
			else
				status = "No text files attached (binary or over 256 KiB)";
			fStatusBar->SetText(status.String());
			break;
		}

		case B_REFS_RECEIVED:
			{
				// Handle result from the Open Project BFilePanel (or Tracker drops)
				// This is very native Haiku behavior (same as Tracker, Pe, etc.)
				entry_ref ref;
				if (message->FindRef("refs", &ref) == B_OK) {
					BEntry entry(&ref, true);
					BPath path;
					if (entry.GetPath(&path) == B_OK) {
						BString projectName = path.Leaf() ? path.Leaf() : "Project";
						BString fullPath = path.Path();
						BMessage open(kMsgOpenProject);
						open.AddString("path", fullPath);
						fController.SendMessage(&open);

						// Update window title — classic Haiku app style
						BString newTitle = "haikode — ";
						newTitle << projectName;
						SetTitle(newTitle.String());

						BString s;
						s << "\n[Project] " << fullPath << "\n";
						_Append(s.String());

						// Refresh the left outline list (Tracker-like project sidebar)
						while (fProjectList->CountItems() > 0)
							delete fProjectList->RemoveItem((int32)0);

						BStringItem* newRoot = new BStringItem(projectName);
						fProjectList->AddItem(newRoot);
						int32 itemCount = 0;
						populate_project_tree(fProjectList, newRoot, path, 2,
							itemCount);
						fProjectList->Expand(newRoot);

						BString status;
						status << "Project: " << projectName << "  •  Native Haiku UI";
						fStatusBar->SetText(status.String());

						_Append("(New provider runs now use this project as their working directory.)\n\n");
					}
				}
			}
			break;

		case MSG_CLEAR:
			fTranscript->SetText("");
			_ResetMeters();
			_Append("Transcript cleared.\n\n");
			break;

		case MSG_PROJECT_INVOKED:
		{
			int32 index = fProjectList->CurrentSelection();
			if (index >= 0) {
				BStringItem* item = static_cast<BStringItem*>(fProjectList->ItemAt(index));
				if (item) {
					BString s;
					s << "\n[Project] Invoked: " << item->Text() << "\n";
					s << "(M1: add to context / open in Pe / read for agent)\n\n";
					_Append(s.String());
				}
			}
			break;
		}

		case 'VSID':
			_Append("[View] Sidebar toggle placeholder (M1 feature).\n");
			break;

		case MSG_SHOW_SETTINGS:
		{
			// Single-instance settings window: the BMessenger stays valid
			// only while the window's looper exists, so if the user closed
			// it we create a fresh one; otherwise bring it to front.
			if (fSettings.IsValid())
				fSettings.SendMessage(kMsgActivateSettings);
			else {
				SettingsWindow* settings
					= new SettingsWindow(BMessenger(this));
				fSettings = BMessenger(settings);
				settings->Show();
			}
			break;
		}

		case MSG_ABOUT:
		{
			BAlert* alert = new BAlert(
				"About haikode",
				"haikode — 100% native BeAPI AI coding agent for Haiku OS\n\n"
				"UI: BWindow • BMenuBar • BSplitView • BOutlineListView\n"
				"      BTextView • BStatusBar • BLayoutBuilder • BFilePanel • ui_color\n\n"
				"Architecture: window posts to AppController (BLooper).\n"
				"Controller owns a real cancellable provider worker.\n"
				"Never blocks the window looper.\n\n"
				"Choose a provider and model in Settings, type a prompt "
				"and press Send. File > Open Project… scopes the agent to "
				"a directory.\n\n"
				"Feels like a first-party Haiku application.",
				"OK");
			alert->SetFlags(alert->Flags() | B_CLOSE_ON_ESCAPE);
			alert->Go();
			break;
		}

		default:
			BWindow::MessageReceived(message);
			break;
	}
}

bool
HaiWindow::QuitRequested()
{
	return BWindow::QuitRequested();
}

void
HaiWindow::_Append(const char* text)
{
	// Explicitly styled rather than inherited: a stylable BTextView carries the
	// last run forward, so plain assistant text after a red diff line would
	// otherwise stay red.
	_AppendStyled(text, ui_color(B_DOCUMENT_TEXT_COLOR), false);
}

void
HaiWindow::_AppendStyled(const char* text, rgb_color color, bool bold,
	const BFont* font)
{
	if (fTranscript == NULL || text == NULL || text[0] == '\0')
		return;

	int32 start = fTranscript->TextLength();
	size_t length = strlen(text);
	fTranscript->Insert(start, text, length);

	BFont styled(font != NULL ? font : (bold ? be_bold_font : be_plain_font));
	if (bold && font != NULL)
		styled.SetFace(B_BOLD_FACE);
	// B_FONT_ALL, not 0: with an empty mode BTextView keeps the previous run's
	// font and only the colour would change.
	fTranscript->SetFontAndColor(start, start + (int32)length, &styled,
		B_FONT_ALL, &color);
	fTranscript->ScrollToOffset(fTranscript->TextLength());
}


rgb_color
HaiWindow::_DimTextColor() const
{
	// Half way between the document text and its background, so secondary
	// lines read as secondary in both the light and the dark system palette
	// (a fixed tint would invert one of them).
	rgb_color text = ui_color(B_DOCUMENT_TEXT_COLOR);
	rgb_color back = ui_color(B_DOCUMENT_BACKGROUND_COLOR);
	rgb_color mixed;
	mixed.red = (uint8)(((int)text.red + back.red) / 2);
	mixed.green = (uint8)(((int)text.green + back.green) / 2);
	mixed.blue = (uint8)(((int)text.blue + back.blue) / 2);
	mixed.alpha = 255;
	return mixed;
}


void
HaiWindow::_AppendDiff(const char* diff)
{
	if (diff == NULL || diff[0] == '\0')
		return;

	rgb_color base = ui_color(B_DOCUMENT_TEXT_COLOR);
	rgb_color added = ui_color(B_SUCCESS_COLOR);
	rgb_color removed = ui_color(B_FAILURE_COLOR);
	rgb_color hunk = _DimTextColor();

	BStringList lines;
	BString(diff).Split("\n", false, lines);
	for (int32 i = 0; i < lines.CountStrings(); i++) {
		BString line = lines.StringAt(i);
		if (i + 1 == lines.CountStrings() && line.IsEmpty())
			break;  // the trailing newline, not a diff line
		line << "\n";
		rgb_color color = base;
		if (line.StartsWith("+++") || line.StartsWith("---")
			|| line.StartsWith("@@"))
			color = hunk;
		else if (line.StartsWith("+"))
			color = added;
		else if (line.StartsWith("-"))
			color = removed;
		_AppendStyled(line.String(), color, false, be_fixed_font);
	}
}


void
HaiWindow::_LogToolActivity(const char* text)
{
	if (fToolList == NULL || fToolLogRoot == NULL || text == NULL)
		return;
	BString entry(text);
	entry.ReplaceAll('\n', ' ');
	entry.Trim();
	if (entry.IsEmpty())
		return;
	fToolList->AddUnder(new BStringItem(entry.String()), fToolLogRoot);
	fToolList->Expand(fToolLogRoot);
}


void
HaiWindow::_ClearChildren(BStringItem* root)
{
	if (fToolList == NULL || root == NULL)
		return;
	while (fToolList->CountItemsUnder(root, true) > 0) {
		BListItem* item = fToolList->ItemUnderAt(root, true, 0);
		if (item == NULL || !fToolList->RemoveItem(item))
			break;
		delete item;
	}
}


void
HaiWindow::_SetTodos(const char* text, const char* summary)
{
	if (fToolList == NULL || fTodoRoot == NULL)
		return;
	// todowrite publishes the whole list every time, so the panel is replaced
	// rather than appended to — otherwise a five-step plan revised twice reads
	// as fifteen steps.
	_ClearChildren(fTodoRoot);

	BString label("Plan");
	if (summary != NULL && summary[0] != '\0')
		label << " — " << summary;
	fTodoRoot->SetText(label.String());
	fToolList->InvalidateItem(fToolList->IndexOf(fTodoRoot));

	BStringList lines;
	BString(text != NULL ? text : "").Split("\n", true, lines);
	for (int32 i = 0; i < lines.CountStrings(); i++)
		fToolList->AddUnder(new BStringItem(lines.StringAt(i).String()),
			fTodoRoot);
	fToolList->Expand(fTodoRoot);
}


void
HaiWindow::_SetContext(float percent, const char* label, const char* tip)
{
	if (fContextBar == NULL)
		return;
	if (percent < 0.0f)
		percent = 0.0f;
	fContextBar->SetBarColor(_ContextColor(percent));
	fContextBar->SetTrailingText(
		label != NULL && label[0] != '\0' ? label : "idle");
	if (tip != NULL && tip[0] != '\0')
		fContextBar->SetToolTip(tip);
	// BStatusBar takes a delta, and it clamps to [0, max] itself, so an
	// over-full window (opencode does not cap its percentage either) simply
	// pins the bar rather than wrapping it.
	fContextBar->Update(percent - fContextBar->CurrentValue());
}


void
HaiWindow::_ResetMeters()
{
	_ClearChildren(fTodoRoot);
	if (fTodoRoot != NULL && fToolList != NULL) {
		fTodoRoot->SetText("Plan");
		fToolList->InvalidateItem(fToolList->IndexOf(fTodoRoot));
	}
	_ClearChildren(fToolLogRoot);
	if (fContextBar != NULL) {
		fContextBar->Reset("Context", "idle");
		fContextBar->SetMaxValue(100.0f);
		fContextBar->SetBarColor(ui_color(B_STATUS_BAR_COLOR));
	}
}


rgb_color
HaiWindow::_ContextColor(float percent) const
{
	// The same two thresholds usage.py uses for its ASCII meter (60/85), so
	// the desktop and the terminal turn amber and red at the same moment.
	rgb_color base = ui_color(B_STATUS_BAR_COLOR);
	rgb_color danger = ui_color(B_FAILURE_COLOR);
	if (percent >= 85.0f)
		return danger;
	if (percent < 60.0f)
		return base;
	rgb_color warn;
	warn.red = (uint8)(((int)base.red + danger.red) / 2);
	warn.green = (uint8)(((int)base.green + danger.green) / 2);
	warn.blue = (uint8)(((int)base.blue + danger.blue) / 2);
	warn.alpha = 255;
	return warn;
}


void
HaiWindow::_SetRunning(bool running)
{
	fRunning = running;
}

void
HaiWindow::_ClearPendingApprovals()
{
	_ClearChildren(fApprovalRoot);
	fApproveOnceButton->SetEnabled(false);
	fApproveAlwaysButton->SetEnabled(false);
	fDenyButton->SetEnabled(false);
}

void
HaiWindow::_AppendStyledLabel(const char* label, bool isUser)
{
	// Bold + slight color variation for recognizability (still uses system
	// fonts where possible).
	rgb_color col = isUser
		? (rgb_color){ 0, 0, 120, 255 }   // dark blue-ish for user (native feel)
		: (rgb_color){ 0, 80, 0, 255 };   // dark green-ish for haikode
	_AppendStyled(label, col, true);
}
