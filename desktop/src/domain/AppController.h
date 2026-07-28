#ifndef HAI_APP_CONTROLLER_H
#define HAI_APP_CONTROLLER_H

#include <Looper.h>
#include <Messenger.h>
#include <OS.h>
#include <String.h>

#include <sys/types.h>

class AppController : public BLooper {
public:
								AppController();
	virtual						~AppController();

	virtual	void				MessageReceived(BMessage* message);

private:
			void				_StartRun(const char* prompt,
									BMessenger sink, int32 generation);
			void				_CancelRun();
			void				_SendApproval(const char* id,
									const char* response);

			BMessenger			fEventSink;
			BString				fProjectPath;
			BString				fSessionName;
			int32				fGeneration;
			thread_id			fWorkerThread;
			pid_t				fChildPid;
			int					fWorkerInputFD;
			bool				fCancelling;
};

#endif
