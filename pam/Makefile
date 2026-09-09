CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -fPIC -fstack-protector-strong -D_FORTIFY_SOURCE=2
LDFLAGS ?= -shared -Wl,-z,relro,-z,now
LIBDIR  ?= /usr/lib/security

pam_glance.so: pam_glance.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $< -lpam

install: pam_glance.so
	install -Dm755 pam_glance.so $(DESTDIR)$(LIBDIR)/pam_glance.so

clean:
	rm -f pam_glance.so

.PHONY: install clean
