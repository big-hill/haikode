#include <Application.h>
#include <Entry.h>
#include <Path.h>
#include <Looper.h>

#include "../domain/AppController.h"
#include "../domain/Messages.h"
#include "../ui/HaiWindow.h"

// M0 native BeAPI application.
// - Proper controller lifecycle (Quit the looper)
// - ArgvReceived + RefsReceived (loop + symlink traversal)
// - No B_ARGV_ONLY so drops from Tracker work

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

		(new HaiWindow(BRect(80, 80, 1100, 760),
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
