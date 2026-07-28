#include "../src/domain/AppController.h"
#include "../src/domain/Messages.h"

#include <Application.h>
#include <Looper.h>
#include <Message.h>
#include <Messenger.h>
#include <OS.h>
#include <String.h>

#include <stdio.h>
#include <stdlib.h>


class TestSink : public BLooper {
public:
	TestSink(BMessenger controller)
		:
		BLooper("haikode controller smoke sink"),
		fDone(create_sem(0, "haikode controller smoke done")),
		fOutcome(0),
		fController(controller),
		fSawApproval(false)
	{
	}

	virtual ~TestSink()
	{
		delete_sem(fDone);
	}

	virtual void MessageReceived(BMessage* message)
	{
		switch (message->what) {
			case kMsgStreamDelta:
			{
				const char* text;
				if (message->FindString("text", &text) == B_OK)
					fText << text;
				break;
			}
			case kMsgRunCompleted:
				fOutcome = 1;
				release_sem(fDone);
				break;
			case kMsgApprovalRequested:
			{
				const char* id;
				if (message->FindString("id", &id) == B_OK) {
					BMessage response(kMsgApprovalResponse);
					response.AddString("id", id);
					response.AddString("response", "once");
					fController.SendMessage(&response);
					fSawApproval = true;
				}
				break;
			}
			case kMsgRunFailed:
			{
				const char* error;
				if (message->FindString("error", &error) == B_OK)
					fError = error;
				fOutcome = -1;
				release_sem(fDone);
				break;
			}
			default:
				BLooper::MessageReceived(message);
		}
	}

	sem_id DoneSem() const { return fDone; }
	int32 Outcome() const { return fOutcome; }
	const BString& Text() const { return fText; }
	const BString& Error() const { return fError; }
	bool SawApproval() const { return fSawApproval; }

private:
	sem_id fDone;
	int32 fOutcome;
	BString fText;
	BString fError;
	BMessenger fController;
	bool fSawApproval;
};


int
main()
{
	setenv("HAI_PYTHONPATH", "/boot/home/haikode", 1);
	// Deliberately removed: the controller has to put PYTHONPATH into the
	// worker's environment itself. It used to do that with setenv() between
	// fork() and exec(), which never reached the exec'd process — so the app
	// only ever worked when whoever launched it had already exported the
	// variable, which Tracker and Deskbar do not.
	unsetenv("PYTHONPATH");
	const char* prompt = getenv("HAI_CONTROLLER_PROMPT");
	const char* expected = getenv("HAI_CONTROLLER_EXPECTED");
	bool approvalSmoke = expected == NULL;
	if (approvalSmoke) {
		prompt = "native controller approval smoke";
		expected = "HAI_NATIVE_CONTROLLER_OK:once";
		setenv("HAI_DESKTOP_TEST_REPLY", "HAI_NATIVE_CONTROLLER_OK", 1);
		setenv("HAI_DESKTOP_TEST_PERMISSION", "once", 1);
	} else {
		unsetenv("HAI_DESKTOP_TEST_REPLY");
		unsetenv("HAI_DESKTOP_TEST_PERMISSION");
	}

	BApplication application("application/x-vnd.haikode-controller-smoke");
	AppController* controller = new AppController();
	TestSink* sink = new TestSink(BMessenger(controller));
	controller->Run();
	sink->Run();

	BMessage promptMessage(kMsgSendPrompt);
	promptMessage.AddString("text", prompt);
	promptMessage.AddMessenger("sink", BMessenger(sink));
	promptMessage.AddInt32("gen", 1);
	controller->PostMessage(&promptMessage);

	status_t waited = acquire_sem_etc(sink->DoneSem(), 1,
		B_RELATIVE_TIMEOUT, 20000000);
	bool passed = waited == B_OK && sink->Outcome() == 1
		&& sink->Text() == expected
		&& (!approvalSmoke || sink->SawApproval());
	if (passed)
		printf("%s\n", expected);
	else
		fprintf(stderr, "controller smoke failed: wait=%ld outcome=%ld text='%s' error='%s'\n",
			(long)waited, (long)sink->Outcome(), sink->Text().String(),
			sink->Error().String());

	if (controller->Lock())
		controller->Quit();
	if (sink->Lock())
		sink->Quit();
	return passed ? 0 : 1;
}
