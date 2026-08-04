#ifndef HAI_WINDOW_H
#define HAI_WINDOW_H

#include <GraphicsDefs.h>
#include <Window.h>
#include <Messenger.h>
#include <String.h>

class BFont;
class BMenu;
class BPopUpMenu;
class BSplitView;
class BTextView;
class BOutlineListView;
class BListView;
class BStatusBar;
class BStringItem;
class BStringView;
class BButton;

class HaiWindow : public BWindow {
public:
								HaiWindow(BRect frame, BMessenger controller);
	virtual	void				MessageReceived(BMessage* message);
	virtual	bool				QuitRequested();

private:
			void				_Append(const char* text);
			void				_AppendStyled(const char* text,
									rgb_color color, bool bold,
									const BFont* font = NULL);
			void				_AppendDiff(const char* diff);
			void				_SetRunning(bool running);
			void				_ClearPendingApprovals();
			void				_AppendStyledLabel(const char* label, bool isUser);
			void				_LogToolActivity(const char* text);
			void				_ClearChildren(BStringItem* root);
			void				_SetTodos(const char* text,
									const char* summary);
			void				_SetContext(float percent,
									const char* label, const char* tip);
			void				_ResetMeters();
			rgb_color			_DimTextColor() const;
			rgb_color			_ContextColor(float percent) const;

			BMessenger			fController;
			BMessenger			fSettings;
			BTextView*			fTranscript;
			BTextView*			fInput;
			BOutlineListView*	fProjectList;
			BListView*			fSessionList;
			BOutlineListView*	fToolList;
			BStringItem*		fApprovalRoot;
			BStringItem*		fTodoRoot;
			BStringItem*		fToolLogRoot;
			BButton*			fApproveOnceButton;
			BButton*			fApproveAlwaysButton;
			BButton*			fDenyButton;
			BStringView*		fInfoView;
			BStatusBar*			fContextBar;
			BStatusBar*			fStatusBar;
			BMenu*				fProviderMenu;
			BSplitView*			fMainSplit;
			BPopUpMenu*			fModelPopup;
			BString				fPendingAttachments;
			BString				fAgentProvider;
			bool				fRunning;
			bool				fStreamed;
			int32				fGeneration;
};

#endif
