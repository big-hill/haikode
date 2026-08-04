#ifndef HAI_SETTINGS_WINDOW_H
#define HAI_SETTINGS_WINDOW_H

#include <InterfaceDefs.h>
#include <Messenger.h>
#include <String.h>
#include <Window.h>

#include <vector>

class BButton;
class BCheckBox;
class BMenuField;
class BPopUpMenu;
class BStringView;
class BTextControl;
class BView;

// Sent by HaiWindow to bring an already-open Settings window to front.
enum : uint32 {
	kMsgActivateSettings = 'sAct',
};

struct ProviderInfo {
	BString	name;
	BString	dialect;
	BString	baseUrl;
	BString	model;
	BString	keyStatus;	// "yes" | "no" | "n/a" | "oauth"
};

// Provider / API key settings window (Haiku HIG style dialog).
// Persistence goes through ConfigBridge -> Python configtool; all
// configtool calls run on a worker thread (spawn_thread) and post
// results back to this window.
class SettingsWindow : public BWindow {
public:
								SettingsWindow(
									BMessenger owner = BMessenger());
	virtual	void				MessageReceived(BMessage* message);

private:
			void				_SeedDefaultProviders();
			void				_RebuildProviderMenu(const char* selectName);
			void				_LoadProviderFields(const char* name);
			ProviderInfo*		_FindProvider(const char* name);
			BString				_CurrentProvider() const;
			void				_ParseProviderList(const BString& output);
			void				_SetStatus(const char* text,
									color_which color);
			void				_SetBusy(bool busy);
			void				_SpawnTool(const BString& args,
									uint32 replyWhat);
			void				_SpawnTools(
									const std::vector<BString>& commands,
									uint32 replyWhat,
									const std::vector<BString>& inputs
										= std::vector<BString>(),
									bigtime_t delay = 0);
			void				_ShowDevicePanel(const BString& url,
									const BString& code);
			void				_EndDeviceLogin(const char* status,
									color_which color);
			void				_PollDeviceLogin(bigtime_t delay);
			void				_OpenDeviceUrl();

			BMenuField*			fProviderField;
			BPopUpMenu*			fProviderMenu;
			BTextControl*		fBaseUrl;
			BTextControl*		fModel;
			BTextControl*		fApiKey;
			BTextControl*		fNewProvider;
			BCheckBox*			fNoKey;
			BStringView*		fStatus;
			BButton*			fTestButton;
			BButton*			fOAuthButton;
			BButton*			fAddProviderButton;
			BButton*			fCancelButton;
			BButton*			fSaveButton;

			// Device-authorization panel: hidden until "Sign in" starts a
			// flow, then it shows the URL and the code while a worker thread
			// polls for the token the local completer is waiting for.
			BView*				fDevicePanel;
			BStringView*		fDeviceHint;
			BTextControl*		fDeviceUrl;
			BTextControl*		fDeviceCode;
			BButton*			fDeviceOpenButton;
			BButton*			fDeviceCancelButton;
			BString				fDeviceProvider;
			BString				fDeviceUrlText;
			bigtime_t			fDeviceDeadline;
			int32				fDeviceGeneration;

			std::vector<ProviderInfo> fProviders;
			BString				fPendingProvider;
			BMessenger			fOwner;
			bool				fBusy;
};

#endif
