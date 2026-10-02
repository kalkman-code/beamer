"""Post user alerts through macOS notifications, including source runs outside an app bundle."""

import secrets


def user_notifications():
    """Load the framework only when a notification is requested."""
    import UserNotifications

    return UserNotifications


_PRESENTER = None


def _presenter_class():
    """The centre's delegate class, made once: macOS keeps a notice from the app in front out of
    sight unless its delegate asks to show it, and Beamer's window is often in front when a crossing
    from its own page is refused."""
    global _PRESENTER
    if _PRESENTER is None:
        import objc
        from Foundation import NSObject

        try:
            protocols = [objc.protocolNamed("UNUserNotificationCenterDelegate")]
        except Exception:
            protocols = []

        class BeamerNoticePresenter(NSObject, protocols=protocols):
            def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, _centre, _note, handler):
                handler(self.options)

        _PRESENTER = BeamerNoticePresenter
    return _PRESENTER


class Notices:
    def __init__(self, logger, framework=user_notifications):
        self.logger = logger
        self.framework = framework
        self._asked = False
        # The centre holds its delegate weakly.
        self._presenter = None

    def _module(self):
        return self.framework() if callable(self.framework) else self.framework

    def ask(self):
        if self._asked:
            return
        self._asked = True
        try:
            framework = self._module()
            centre = framework.UNUserNotificationCenter.currentNotificationCenter()
            self._presenter = _presenter_class().alloc().init()
            self._presenter.options = (framework.UNNotificationPresentationOptionBanner
                                       | framework.UNNotificationPresentationOptionList
                                       | framework.UNNotificationPresentationOptionSound)
            centre.setDelegate_(self._presenter)
            options = framework.UNAuthorizationOptionAlert | framework.UNAuthorizationOptionSound
            centre.requestAuthorizationWithOptions_completionHandler_(options, lambda *_args: None)
        except Exception:
            self.logger.info("Could not request notification authorisation", exc_info=True)

    def post(self, title, body) -> bool:
        self.ask()
        try:
            framework = self._module()
            centre = framework.UNUserNotificationCenter.currentNotificationCenter()
            content = framework.UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(body)
            content.setSound_(framework.UNNotificationSound.defaultSound())
            request = framework.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                secrets.token_urlsafe(24), content, None
            )
            centre.addNotificationRequest_withCompletionHandler_(request, None)
            return True
        except Exception:
            self.logger.info("Could not show notification %r: %r", title, body, exc_info=True)
            return False
