CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -fstack-protector-strong -D_FORTIFY_SOURCE=2
LDFLAGS ?= -Wl,-z,relro,-z,now
LIBDIR  ?= /usr/lib/security

# A PAM module is a shared object whatever the environment's flags say, so
# these are appended rather than defaulted: makepkg exports its own CFLAGS
# and LDFLAGS, and without this the link produced an executable with no main.
override CFLAGS  += -fPIC
override LDFLAGS += -shared

pam_glance.so: pam_glance.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $< -lpam

install: pam_glance.so
	install -Dm755 pam_glance.so $(DESTDIR)$(LIBDIR)/pam_glance.so

clean:
	rm -f pam_glance.so

.PHONY: install clean
