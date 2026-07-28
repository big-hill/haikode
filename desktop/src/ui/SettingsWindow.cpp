#include "SettingsWindow.h"
#include "../domain/ConfigBridge.h"

#include <Button.h>
#include <CheckBox.h>
#include <Font.h>
#include <LayoutBuilder.h>
#include <MenuField.h>
#include <MenuItem.h>
#include <OS.h>
#include <PopUpMenu.h>
#include <Roster.h>
#include <StringList.h>
#include <StringView.h>
#include <TextControl.h>
#include <TextView.h>
#include <UTF8.h>
#include <View.h>

#include <string.h>

// Native Haiku settings dialog (HIG):
// - BLayoutBuilder grid with CreateLabelLayoutItem() for aligned labels
// - B_USE_WINDOW_SPACING insets, B_USE_DEFAULT_SPACING gaps
// - "Test | glue | Cancel Save" button row, Save as default button
// - ui_color()-based colors only (via SetHighUIColor), be_plain_font
// - Closes itself only (no B_QUIT_ON_WINDOW_CLOSE), Escape closes

enum : uint32 {
	kMsgProviderSelected = 'sPrv',
	kMsgTest             = 'sTst',
	kMsgOAuth            = 'sOAu',
	kMsgAddProvider      = 'sAdd',
	kMsgSave             = 'sSav',
	kMsgProvidersLoaded  = 'sPLd',
	kMsgTestResult       = 'sTRs',
	kMsgOAuthResult      = 'sORs',
	kMsgAddProviderResult = 'sARs',
	kMsgSaveResult       = 'sSRs',
	kMsgDeviceOpen       = 'sDOp',
	kMsgDeviceCancel     = 'sDCa',
	kMsgDevicePollResult = 'sDPr',
};

// How long the window keeps watching for a device authorization, and how long
// it waits between checks. The code the provider issued expires on its own
// (5-10 minutes), so watching for longer only wastes requests.
static const bigtime_t kDeviceWindow = 10 * 60 * 1000000LL;
static const bigtime_t kDeviceInterval = 4 * 1000000LL;


// #pragma mark - worker thread


// Runs configtool commands off the window thread and posts the result back.
struct ToolTask {
	BMessenger				target;
	uint32					replyWhat;
	std::vector<BString>	commands;
	std::vector<BString>	inputs;
	bigtime_t				delay;
	int32					generation;
};


static int32
run_tool_task(void* data)
{
	ToolTask* task = static_cast<ToolTask*>(data);

	// Device polling waits here rather than on the window thread: the looper
	// has to stay free to redraw and to accept a Cancel while we wait.
	if (task->delay > 0)
		snooze(task->delay);

	int exitCode = -1;
	BString output;
	for (size_t i = 0; i < task->commands.size(); i++) {
		if (i < task->inputs.size() && !task->inputs[i].IsEmpty())
			output = ConfigBridge::RunConfigToolWithInput(
				task->commands[i], task->inputs[i], &exitCode);
		else
			output = ConfigBridge::RunConfigTool(task->commands[i], &exitCode);
		if (exitCode != 0)
			break;	// stop at first failure, report its output
	}

	BMessage reply(task->replyWhat);
	reply.AddString("output", output);
	reply.AddInt32("exit", exitCode);
	reply.AddInt32("gen", task->generation);
	task->target.SendMessage(&reply);

	delete task;
	return 0;
}


// The user code out of `configtool oauth-start`. A fourth TSV field is used
// when the tool grows one; until then the code is read back out of the
// instruction sentence it is folded into ("Enter code ABCD-1234; ...").
static BString
device_code_from(const BStringList& fields)
{
	if (fields.CountStrings() > 3 && !fields.StringAt(3).IsEmpty())
		return fields.StringAt(3);

	BString instructions = fields.CountStrings() > 2 ? fields.StringAt(2)
		: BString("");
	static const char* kPrefix = "Enter code ";
	int32 start = instructions.FindFirst(kPrefix);
	if (start < 0)
		return BString("");
	start += (int32)strlen(kPrefix);
	int32 end = instructions.FindFirst(";", start);
	if (end < 0)
		end = instructions.Length();

	BString code;
	instructions.CopyInto(code, start, end - start);
	code.Trim();
	return code;
}


// #pragma mark - SettingsWindow


SettingsWindow::SettingsWindow()
	:
	BWindow(BRect(0, 0, 560, 330), "Settings", B_TITLED_WINDOW,
		B_AUTO_UPDATE_SIZE_LIMITS | B_NOT_ZOOMABLE | B_CLOSE_ON_ESCAPE),
	fDeviceDeadline(0),
	fDeviceGeneration(0),
	fBusy(false)
{
	fProviderMenu = new BPopUpMenu("provider");
	fProviderField = new BMenuField("providerField", "Provider:",
		fProviderMenu);

	fBaseUrl = new BTextControl("baseUrl", "Base URL:", "", NULL);
	fModel = new BTextControl("model", "Model:", "", NULL);

	fApiKey = new BTextControl("apiKey", "API key:", "", NULL);
	fApiKey->TextView()->HideTyping(true);
	fApiKey->SetToolTip("Leave empty to keep the stored key unchanged");
	fNewProvider = new BTextControl("newProvider", "New provider:", "", NULL);
	fNewProvider->SetToolTip("Name for a custom OpenAI-compatible or Ollama endpoint");
	fNoKey = new BCheckBox("noKey", "No API key", NULL);

	fStatus = new BStringView("status", "Loading providers" B_UTF8_ELLIPSIS);
	fStatus->SetFont(be_plain_font);
	fStatus->SetHighUIColor(B_PANEL_TEXT_COLOR);

	fTestButton = new BButton("test", "Test", new BMessage(kMsgTest));
	fOAuthButton = new BButton("oauth", "Sign in" B_UTF8_ELLIPSIS,
		new BMessage(kMsgOAuth));
	fAddProviderButton = new BButton("addProvider", "Add",
		new BMessage(kMsgAddProvider));
	fCancelButton = new BButton("cancel", "Cancel",
		new BMessage(B_QUIT_REQUESTED));
	fSaveButton = new BButton("save", "Save", new BMessage(kMsgSave));

	// Device sign-in panel. Both fields are text controls rather than labels
	// so the URL and the code can be selected and copied — on Haiku the
	// browser may well be on another machine.
	fDeviceHint = new BStringView("deviceHint",
		"Open the address below and enter the code:");
	fDeviceHint->SetFont(be_plain_font);
	fDeviceHint->SetHighUIColor(B_PANEL_TEXT_COLOR);

	fDeviceUrl = new BTextControl("deviceUrl", "Address:", "", NULL);
	fDeviceUrl->TextView()->MakeEditable(false);
	fDeviceCode = new BTextControl("deviceCode", "Code:", "", NULL);
	fDeviceCode->TextView()->MakeEditable(false);
	BFont codeFont(be_fixed_font);
	codeFont.SetSize(be_plain_font->Size() * 1.3f);
	fDeviceCode->TextView()->SetFontAndColor(&codeFont);

	fDeviceOpenButton = new BButton("deviceOpen", "Open in browser",
		new BMessage(kMsgDeviceOpen));
	fDeviceCancelButton = new BButton("deviceCancel", "Stop waiting",
		new BMessage(kMsgDeviceCancel));

	fDevicePanel = new BView("devicePanel", B_WILL_DRAW);
	BLayoutBuilder::Group<>(fDevicePanel, B_VERTICAL, B_USE_SMALL_SPACING)
		.Add(fDeviceHint)
		.AddGrid(B_USE_DEFAULT_SPACING, B_USE_SMALL_SPACING)
			.Add(fDeviceUrl->CreateLabelLayoutItem(), 0, 0)
			.Add(fDeviceUrl->CreateTextViewLayoutItem(), 1, 0)
			.Add(fDeviceCode->CreateLabelLayoutItem(), 0, 1)
			.Add(fDeviceCode->CreateTextViewLayoutItem(), 1, 1)
		.End()
		.AddGroup(B_HORIZONTAL, B_USE_SMALL_SPACING)
			.Add(fDeviceOpenButton)
			.Add(fDeviceCancelButton)
			.AddGlue()
		.End()
	.End();

	// Reasonable field width derived from the system font (no pixel
	// constants; scales with font size per HIG).
	float fieldWidth = be_plain_font->StringWidth("M") * 28;
	fBaseUrl->SetExplicitMinSize(BSize(fieldWidth, B_SIZE_UNSET));

	BLayoutBuilder::Group<>(this, B_VERTICAL, B_USE_DEFAULT_SPACING)
		.SetInsets(B_USE_WINDOW_SPACING)
		.AddGrid(B_USE_DEFAULT_SPACING, B_USE_SMALL_SPACING)
			.Add(fProviderField->CreateLabelLayoutItem(), 0, 0)
			.Add(fProviderField->CreateMenuBarLayoutItem(), 1, 0)
			.Add(fBaseUrl->CreateLabelLayoutItem(), 0, 1)
			.Add(fBaseUrl->CreateTextViewLayoutItem(), 1, 1)
			.Add(fModel->CreateLabelLayoutItem(), 0, 2)
			.Add(fModel->CreateTextViewLayoutItem(), 1, 2)
			.Add(fApiKey->CreateLabelLayoutItem(), 0, 3)
			.Add(fApiKey->CreateTextViewLayoutItem(), 1, 3)
		.End()
		.AddGroup(B_HORIZONTAL, B_USE_DEFAULT_SPACING)
			.Add(fNewProvider)
			.Add(fNoKey)
			.Add(fAddProviderButton)
		.End()
		.Add(fDevicePanel)
		.Add(fStatus)
		.AddGroup(B_HORIZONTAL, B_USE_DEFAULT_SPACING)
			.Add(fTestButton)
			.Add(fOAuthButton)
			.AddGlue()
			.Add(fCancelButton)
			.Add(fSaveButton)
		.End()
	.End();

	// Hidden until a flow starts: a layout skips an invisible view, so the
	// dialog keeps its ordinary height until there is a code to show.
	fDevicePanel->Hide();

	SetDefaultButton(fSaveButton);

	// Start with the hardcoded M0 provider set, then refresh from
	// configtool in the background (it may not be installed yet).
	_SeedDefaultProviders();
	_RebuildProviderMenu(fProviders.empty() ? NULL
		: fProviders[0].name.String());
	if (!fProviders.empty())
		_LoadProviderFields(fProviders[0].name.String());

	_SpawnTool("list-providers", kMsgProvidersLoaded);

	CenterOnScreen();
	fBaseUrl->MakeFocus(true);
}


void
SettingsWindow::MessageReceived(BMessage* message)
{
	switch (message->what) {
		case kMsgActivateSettings:
			Activate();
			break;

		case kMsgProviderSelected:
		{
			const char* name;
			if (message->FindString("name", &name) == B_OK)
				_LoadProviderFields(name);
			break;
		}

		case kMsgTest:
		{
			if (fBusy)
				break;
			BString provider = _CurrentProvider();
			if (provider.IsEmpty()) {
				_SetStatus("No provider selected.", B_FAILURE_COLOR);
				break;
			}

			_SetBusy(true);
			BString status("Testing ");
			status << provider << B_UTF8_ELLIPSIS
				<< " (unsaved changes are not tested)";
			_SetStatus(status.String(), B_PANEL_TEXT_COLOR);

			BString args("test ");
			args << ConfigBridge::ShellQuote(provider);
			_SpawnTool(args, kMsgTestResult);
			break;
		}

		case kMsgTestResult:
		{
			_SetBusy(false);
			BString output = message->GetString("output", "");
			int32 exitCode = message->GetInt32("exit", -1);
			if (output.IsEmpty())
				output = exitCode == 0 ? "OK" : "FAIL (no output)";
			_SetStatus(output.String(),
				exitCode == 0 ? B_SUCCESS_COLOR : B_FAILURE_COLOR);
			break;
		}

		case kMsgOAuth:
		{
			if (fBusy)
				break;
			BString provider = _CurrentProvider();
			ProviderInfo* info = _FindProvider(provider.String());
			if (info == NULL || info->keyStatus != "oauth") {
				_SetStatus("This provider does not use subscription OAuth.",
					B_FAILURE_COLOR);
				break;
			}
			// Claimed before the tool runs so a late reply from an earlier
			// attempt cannot reopen the panel on top of this one.
			fDeviceGeneration++;
			fDeviceProvider = provider;
			_SetBusy(true);
			_SetStatus("Starting secure device login locally on Haiku" B_UTF8_ELLIPSIS,
				B_PANEL_TEXT_COLOR);
			BString args("oauth-start ");
			args << ConfigBridge::ShellQuote(provider);
			_SpawnTool(args, kMsgOAuthResult);
			break;
		}

		case kMsgOAuthResult:
		{
			_SetBusy(false);
			if (message->GetInt32("gen", -1) != fDeviceGeneration)
				break;
			int32 exitCode = message->GetInt32("exit", -1);
			BString output = message->GetString("output", "");
			if (exitCode != 0) {
				fDeviceProvider.Truncate(0);
				_SetStatus(output.IsEmpty() ? "Could not start OAuth." : output.String(),
					B_FAILURE_COLOR);
				break;
			}
			BStringList fields;
			output.Trim();
			output.Split("\t", false, fields);
			if (fields.CountStrings() < 1 || fields.StringAt(0).IsEmpty()) {
				fDeviceProvider.Truncate(0);
				_SetStatus("The provider returned no authorization URL.", B_FAILURE_COLOR);
				break;
			}
			_ShowDevicePanel(fields.StringAt(0), device_code_from(fields));
			break;
		}

		case kMsgDeviceOpen:
			_OpenDeviceUrl();
			break;

		case kMsgDeviceCancel:
			// The detached completer keeps its own timer; all this stops is
			// the watching, so say that rather than implying a revocation.
			_EndDeviceLogin("Stopped watching. The code stays valid until it "
				"expires; press Sign in again to resume.", B_PANEL_TEXT_COLOR);
			break;

		case kMsgDevicePollResult:
		{
			// A reply from an abandoned flow (cancelled, or a second Sign in)
			// must not revive the panel.
			if (message->GetInt32("gen", -1) != fDeviceGeneration
				|| fDeviceProvider.IsEmpty())
				break;
			if (message->GetInt32("exit", -1) == 0) {
				BString done("Signed in to ");
				done << fDeviceProvider << ". The token is stored locally on "
					"Haiku.";
				_EndDeviceLogin(done.String(), B_SUCCESS_COLOR);
				// Refresh the key column so the row stops saying "no token".
				_SpawnTool("list-providers", kMsgProvidersLoaded);
				break;
			}
			if (system_time() >= fDeviceDeadline) {
				_EndDeviceLogin("The device code expired before it was "
					"approved. Press Sign in to get a new one.",
					B_FAILURE_COLOR);
				break;
			}
			_PollDeviceLogin(kDeviceInterval);
			break;
		}

		case kMsgAddProvider:
		{
			if (fBusy)
				break;
			BString name(fNewProvider->Text());
			name.Trim();
			if (name.IsEmpty() || fBaseUrl->Text()[0] == '\0') {
				_SetStatus("Enter a provider name and Base URL first.",
					B_FAILURE_COLOR);
				break;
			}
			BString args("add-provider ");
			args << ConfigBridge::ShellQuote(name) << " openai "
				<< ConfigBridge::ShellQuote(fBaseUrl->Text()) << " "
				<< ConfigBridge::ShellQuote(fModel->Text()) << " "
				<< (fNoKey->Value() == B_CONTROL_ON ? "false" : "true");
			fPendingProvider = name;
			_SetBusy(true);
			_SetStatus("Adding provider" B_UTF8_ELLIPSIS, B_PANEL_TEXT_COLOR);
			_SpawnTool(args, kMsgAddProviderResult);
			break;
		}

		case kMsgAddProviderResult:
		{
			_SetBusy(false);
			if (message->GetInt32("exit", -1) != 0) {
				BString output = message->GetString("output", "");
				_SetStatus(output.IsEmpty() ? "Could not add provider."
					: output.String(), B_FAILURE_COLOR);
				fPendingProvider.Truncate(0);
				break;
			}
			fNewProvider->SetText("");
			_SpawnTool("list-providers", kMsgProvidersLoaded);
			break;
		}

		case kMsgSave:
		{
			if (fBusy)
				break;
			BString provider = _CurrentProvider();
			if (provider.IsEmpty()) {
				_SetStatus("No provider selected.", B_FAILURE_COLOR);
				break;
			}

			std::vector<BString> commands;
			std::vector<BString> inputs;

			BString setDefault("set default_provider ");
			setDefault << ConfigBridge::ShellQuote(provider);
			commands.push_back(setDefault);
			inputs.push_back("");

			BString setUrl("set ");
			setUrl << ConfigBridge::ShellQuote(
					BString("providers.") << provider << ".base_url")
				<< " " << ConfigBridge::ShellQuote(fBaseUrl->Text());
			commands.push_back(setUrl);
			inputs.push_back("");

			BString setModel("set ");
			setModel << ConfigBridge::ShellQuote(
					BString("providers.") << provider << ".model")
				<< " " << ConfigBridge::ShellQuote(fModel->Text());
			commands.push_back(setModel);
			inputs.push_back("");

			ProviderInfo* selected = _FindProvider(provider.String());
			if (selected != NULL && selected->keyStatus != "oauth"
				&& selected->keyStatus != "n/a"
				&& fApiKey->Text() != NULL && fApiKey->Text()[0] != '\0') {
				BString setKey("set-key-stdin ");
				setKey << ConfigBridge::ShellQuote(provider);
				commands.push_back(setKey);
				inputs.push_back(fApiKey->Text());
			}

			_SetBusy(true);
			_SetStatus("Saving" B_UTF8_ELLIPSIS, B_PANEL_TEXT_COLOR);
			_SpawnTools(commands, kMsgSaveResult, inputs);
			break;
		}

		case kMsgSaveResult:
		{
			_SetBusy(false);
			int32 exitCode = message->GetInt32("exit", -1);
			if (exitCode == 0) {
				// Keep the cache coherent, then close — this window only
				// closes itself; the app keeps running.
				ProviderInfo* info
					= _FindProvider(_CurrentProvider().String());
				if (info != NULL) {
					info->baseUrl = fBaseUrl->Text();
					info->model = fModel->Text();
					if (fApiKey->Text()[0] != '\0')
						info->keyStatus = "yes";
				}
				PostMessage(B_QUIT_REQUESTED);
			} else {
				BString output = message->GetString("output", "");
				BString status("Save failed: ");
				status << (output.IsEmpty() ? "configtool error" : output);
				_SetStatus(status.String(), B_FAILURE_COLOR);
			}
			break;
		}

		case kMsgProvidersLoaded:
		{
			int32 exitCode = message->GetInt32("exit", -1);
			BString output = message->GetString("output", "");
			if (exitCode != 0) {
				_SetStatus("configtool unavailable — using built-in "
					"provider list.", B_FAILURE_COLOR);
				break;
			}

			BString selected = fPendingProvider.IsEmpty()
				? _CurrentProvider() : fPendingProvider;
			fPendingProvider.Truncate(0);
			_ParseProviderList(output);
			if (_FindProvider(selected.String()) == NULL
				&& !fProviders.empty())
				selected = fProviders[0].name;
			_RebuildProviderMenu(selected.String());
			_LoadProviderFields(selected.String());
			break;
		}

		default:
			BWindow::MessageReceived(message);
			break;
	}
}


void
SettingsWindow::_SeedDefaultProviders()
{
	// Hardcoded M0 fallback; replaced by `configtool list-providers`
	// output when the tool is available.
	static const char* kNames[] = { "ollama", "ollama-local", "chatgpt",
		"supergrok", "xai", "anthropic", "openai" };
	fProviders.clear();
	for (size_t i = 0; i < sizeof(kNames) / sizeof(kNames[0]); i++) {
		ProviderInfo info;
		info.name = kNames[i];
		fProviders.push_back(info);
	}
}


void
SettingsWindow::_RebuildProviderMenu(const char* selectName)
{
	while (fProviderMenu->CountItems() > 0)
		delete fProviderMenu->RemoveItem((int32)0);

	for (size_t i = 0; i < fProviders.size(); i++) {
		BMessage* selection = new BMessage(kMsgProviderSelected);
		selection->AddString("name", fProviders[i].name);
		BMenuItem* item = new BMenuItem(fProviders[i].name.String(),
			selection);
		if (selectName != NULL && fProviders[i].name == selectName)
			item->SetMarked(true);
		fProviderMenu->AddItem(item);
	}
}


void
SettingsWindow::_LoadProviderFields(const char* name)
{
	ProviderInfo* info = _FindProvider(name);
	if (info == NULL)
		return;

	fBaseUrl->SetText(info->baseUrl.String());
	fModel->SetText(info->model.String());
	fApiKey->SetText("");
	// Not while a device login is in flight: its panel is showing a code for
	// a different provider than the one just selected.
	fOAuthButton->SetEnabled(info->keyStatus == "oauth"
		&& fDeviceProvider.IsEmpty() && !fBusy);
	fApiKey->SetEnabled(info->keyStatus != "oauth"
		&& info->keyStatus != "n/a");

	BString status(info->name);
	if (info->keyStatus == "yes")
		status << ": API key is set.";
	else if (info->keyStatus == "no")
		status << ": no API key stored.";
	else if (info->keyStatus == "n/a")
		status << ": no API key needed.";
	else if (info->keyStatus == "oauth")
		status << ": subscription OAuth is stored locally on Haiku.";
	else
		status << " selected.";
	_SetStatus(status.String(), B_PANEL_TEXT_COLOR);
}


ProviderInfo*
SettingsWindow::_FindProvider(const char* name)
{
	if (name == NULL)
		return NULL;
	for (size_t i = 0; i < fProviders.size(); i++) {
		if (fProviders[i].name == name)
			return &fProviders[i];
	}
	return NULL;
}


BString
SettingsWindow::_CurrentProvider() const
{
	BMenuItem* marked = fProviderMenu->FindMarked();
	return BString(marked != NULL ? marked->Label() : "");
}


void
SettingsWindow::_ParseProviderList(const BString& output)
{
	// Lines: name, dialect, base_url, model, key status
	std::vector<ProviderInfo> parsed;

	BStringList lines;
	output.Split("\n", true, lines);
	for (int32 i = 0; i < lines.CountStrings(); i++) {
		BStringList fields;
		lines.StringAt(i).Split("\t", false, fields);
		if (fields.CountStrings() < 1 || fields.StringAt(0).IsEmpty())
			continue;

		ProviderInfo info;
		info.name = fields.StringAt(0);
		info.dialect = fields.CountStrings() > 1 ? fields.StringAt(1) : "";
		info.baseUrl = fields.CountStrings() > 2 ? fields.StringAt(2) : "";
		info.model = fields.CountStrings() > 3 ? fields.StringAt(3) : "";
		info.keyStatus = fields.CountStrings() > 4 ? fields.StringAt(4) : "";
		if (info.keyStatus.StartsWith("key:"))
			info.keyStatus.Remove(0, 4);
		parsed.push_back(info);
	}

	if (!parsed.empty())
		fProviders = parsed;
}


void
SettingsWindow::_SetStatus(const char* text, color_which color)
{
	BString oneLine(text != NULL ? text : "");
	oneLine.ReplaceAll("\n", "  ");
	oneLine.Trim();

	fStatus->SetToolTip(oneLine.String());
	if (oneLine.CountChars() > 120) {
		oneLine.TruncateChars(120);
		oneLine << B_UTF8_ELLIPSIS;
	}

	fStatus->SetHighUIColor(color);
	fStatus->SetText(oneLine.String());
}


void
SettingsWindow::_SetBusy(bool busy)
{
	fBusy = busy;
	fTestButton->SetEnabled(!busy);
	ProviderInfo* info = _FindProvider(_CurrentProvider().String());
	// A device login already in flight owns the button until it ends: two
	// concurrent flows would leave two codes and only one of them working.
	fOAuthButton->SetEnabled(!busy && info != NULL
		&& info->keyStatus == "oauth" && fDeviceProvider.IsEmpty());
	fAddProviderButton->SetEnabled(!busy);
	fSaveButton->SetEnabled(!busy);
}


void
SettingsWindow::_ShowDevicePanel(const BString& url, const BString& code)
{
	fDeviceUrlText = url;
	fDeviceUrl->SetText(url.String());
	fDeviceCode->SetText(code.IsEmpty() ? "(shown on the page)" : code.String());
	fDeviceDeadline = system_time() + kDeviceWindow;

	if (fDevicePanel->IsHidden())
		fDevicePanel->Show();
	fDeviceCancelButton->SetEnabled(true);
	fDeviceOpenButton->SetEnabled(true);
	fOAuthButton->SetEnabled(false);

	BString status("Waiting for ");
	status << fDeviceProvider << " to approve this device"
		<< B_UTF8_ELLIPSIS;
	_SetStatus(status.String(), B_PANEL_TEXT_COLOR);

	_OpenDeviceUrl();
	// The first check is immediate: the browser may already be signed in, in
	// which case approval takes a second and the panel should not linger.
	_PollDeviceLogin(0);
}


void
SettingsWindow::_EndDeviceLogin(const char* status, color_which color)
{
	// Bumping the generation is what drops any poll still in the air.
	fDeviceGeneration++;
	fDeviceProvider.Truncate(0);
	fDeviceDeadline = 0;
	if (!fDevicePanel->IsHidden())
		fDevicePanel->Hide();
	_SetStatus(status, color);
	_SetBusy(fBusy);
}


void
SettingsWindow::_PollDeviceLogin(bigtime_t delay)
{
	if (fDeviceProvider.IsEmpty())
		return;
	// `test` is the honest question: it asks whether a usable token is stored
	// now, which is exactly what the detached completer is racing to produce.
	BString args("test ");
	args << ConfigBridge::ShellQuote(fDeviceProvider);
	std::vector<BString> commands;
	commands.push_back(args);
	_SpawnTools(commands, kMsgDevicePollResult, std::vector<BString>(), delay);
}


void
SettingsWindow::_OpenDeviceUrl()
{
	if (fDeviceUrlText.IsEmpty())
		return;
	const char* arguments[1];
	arguments[0] = fDeviceUrlText.String();
	if (be_roster->Launch("text/html", 1, arguments) != B_OK)
		fDeviceHint->SetText(
			"No browser here — open this address on another machine:");
}


void
SettingsWindow::_SpawnTool(const BString& args, uint32 replyWhat)
{
	std::vector<BString> commands;
	commands.push_back(args);
	_SpawnTools(commands, replyWhat);
}


void
SettingsWindow::_SpawnTools(const std::vector<BString>& commands,
	uint32 replyWhat, const std::vector<BString>& inputs, bigtime_t delay)
{
	ToolTask* task = new ToolTask();
	task->target = BMessenger(this);
	task->replyWhat = replyWhat;
	task->commands = commands;
	task->inputs = inputs;
	task->delay = delay;
	task->generation = fDeviceGeneration;

	thread_id thread = spawn_thread(run_tool_task, "configtool bridge",
		B_NORMAL_PRIORITY, task);
	if (thread >= 0)
		resume_thread(thread);
	else {
		delete task;
		_SetBusy(false);
		_SetStatus("Could not spawn configtool thread.", B_FAILURE_COLOR);
	}
}
