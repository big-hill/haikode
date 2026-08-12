#include <Application.h>
#include <Entry.h>
#include <List.h>
#include <Path.h>
#include <Looper.h>
#include <Roster.h>
#include <Screen.h>

#include "../domain/AppController.h"
#include "../domain/Messages.h"
#include "../ui/HaiWindow.h"

// M0 native BeAPI application.
// - Proper controller lifecycle (Quit the looper)
// - ArgvReceived + RefsReceived (loop + symlink traversal)
// - No B_ARGV_ONLY so drops from Tracker work

static BRect
initial_window_frame()
{
	BRect frame(80, 80, 1100, 760);
	BScreen screen;
	if (screen.IsValid()) {
		BRect bounds = screen.Frame();
		float width = bounds.Width() * 0.78f;
		if (width < 720.0f)
			width = 720.0f;
		else if (width > 1020.0f)
			width = 1020.0f;
		float height = bounds.Height() * 0.75f;
		if (height < 520.0f)
			height = 520.0f;
		else if (height > 680.0f)
			height = 680.0f;
		frame.right = frame.left + width;
		frame.bottom = frame.top + height;

		// Each B_MULTIPLE_LAUNCH process has no access to its siblings'
		// BWindows, but the roster does expose how many sibling apps exist.
		// Give every live instance the next screen-sized cascade slot. This
		// avoids the misleading appearance that New Window did nothing.
		BList teams;
		if (be_roster != NULL)
			be_roster->GetAppList("application/x-vnd.haikode", &teams);
		int32 ordinal = teams.CountItems() - 1;
		if (ordinal < 0)
			ordinal = 0;
		const float step = 28.0f;
		float horizontalRoom = bounds.right - frame.right - 16.0f;
		float verticalRoom = bounds.bottom - frame.bottom - 16.0f;
		int32 columns = horizontalRoom > 0
			? (int32)(horizontalRoom / step) + 1 : 1;
		int32 rows = verticalRoom > 0
			? (int32)(verticalRoom / step) + 1 : 1;
		int32 slots = columns * rows;
		int32 slot = slots > 0 ? ordinal % slots : 0;
		frame.OffsetBy((slot % columns) * step,
			(slot / columns) * step);
	}
	return frame;
}

class HaiApplication : public BApplication {
public:
	HaiApplication()
		:
		BApplication("application/x-vnd.haikode"),
		fController(NULL)
	{
	}

	virtual void ReadyToRun()
	{
		fController = new AppController();
		fController->Run();

		(new HaiWindow(initial_window_frame(),
			BMessenger(fController)))->Show();
	}

	virtual void RefsReceived(BMessage* message)
	{
		// Support multiple dropped items + symlinks (Tracker behavior)
		entry_ref ref;
		for (int32 i = 0; message->FindRef("refs", i, &ref) == B_OK; i++) {
			BEntry entry(&ref, true);  // traverse symlink
			BPath path;
			if (entry.GetPath(&path) == B_OK)
				_OpenProject(path.Path());
		}
	}

	virtual void ArgvReceived(int32 argc, char** argv)
	{
		// Terminal launch with path(s)
		for (int32 i = 1; i < argc; i++)
			_OpenProject(argv[i]);
	}

	virtual bool QuitRequested()
	{
		bool result = BApplication::QuitRequested();
		if (result && fController != NULL) {
			if (fController->Lock()) {
				fController->Quit();   // this deletes the looper
				fController = NULL;
			}
		}
		return result;
	}

private:
	void _OpenProject(const char* path)
	{
		if (path == NULL || fController == NULL) return;
		BMessage open(kMsgOpenProject);
		open.AddString("path", path);
		fController->PostMessage(&open);
	}

	AppController* fController;
};

int
main()
{
	HaiApplication app;
	app.Run();
	return 0;
}
