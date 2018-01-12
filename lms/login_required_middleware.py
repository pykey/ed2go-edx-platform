from re import compile

from django.conf import settings
from django.http import HttpResponseRedirect


EXEMPT_URLS = [compile(settings.ED2GO_LOGIN_URL.lstrip('/'))]
if hasattr(settings, 'LOGIN_EXEMPT_URLS'):
    EXEMPT_URLS += [compile(expr) for expr in settings.LOGIN_EXEMPT_URLS]


    class LoginRequiredMiddleware:
        """
        Middleware that requires a user to be authenticated to view any page other
        than ED2GO_LOGIN_URL. Exemptions to this requirement can optionally be specified
        in settings via a list of regular expressions in LOGIN_EXEMPT_URLS (which
        you can copy from your urls.py).
        Requires authentication middleware and template context processors to be
        loaded. You'll get an error if they aren't.
        """
        def process_request(self, request):
            assert hasattr(request, 'user'), "The Login Required middleware\
     requires authentication middleware to be installed. Edit your\
     MIDDLEWARE_CLASSES setting to insert\
     'django.contrib.auth.middleware.AuthenticationMiddleware'. If that doesn't\
     work, ensure your TEMPLATE_CONTEXT_PROCESSORS setting includes\
     'django.core.context_processors.auth'."
            # If ED2GO is true, every HTTP request on the LMS will be redirected to ED2GO_LOGIN_URL
            # declared in commmon.py(settings file)
            if settings.ED2GO:
                if not request.user.is_authenticated():
                    path = request.path_info.lstrip('/')
                    if not any(m.match(path) for m in EXEMPT_URLS):
                        return HttpResponseRedirect(settings.ED2GO_LOGIN_URL)
