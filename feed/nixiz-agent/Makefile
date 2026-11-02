# SPDX-License-Identifier: GPL-2.0-only
# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
NIXIZ_SOURCE_DIR:=$(abspath $(dir $(realpath $(lastword $(MAKEFILE_LIST))))/../..)
include $(TOPDIR)/rules.mk

PKG_NAME:=nixiz-agent
PKG_VERSION:=0.1.0
PKG_RELEASE:=1
PKG_LICENSE:=GPL-2.0-only
PKGARCH:=all

include $(INCLUDE_DIR)/package.mk

define Package/nixiz-agent
  SECTION:=admin
  CATEGORY:=Administration
  TITLE:=Transactional declarative OpenWrt configuration agent
  DEPENDS:=+jq +uci +ubus +flock +coreutils-timeout
endef

define Package/nixiz-agent/description
  Configuration transactions, identity enrollment, independent rollback and
  recovery for nixiz. Installation preserves existing network and credentials.
endef

define Package/nixiz-agent/conffiles
/etc/nixiz/identity.json
/etc/nixiz/managed.json
/etc/nixiz/root-password-hash
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)/agent
	$(CP) $(NIXIZ_SOURCE_DIR)/agent/. $(PKG_BUILD_DIR)/agent/
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/nixiz-agent/install
	$(INSTALL_DIR) $(1)/usr/libexec/nixiz $(1)/usr/share/nixiz $(1)/etc/init.d $(1)/etc/nixiz
	chmod 0700 $(1)/etc/nixiz
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/agent/agent.sh $(1)/usr/libexec/nixiz/agent
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/agent/guard.init $(1)/etc/init.d/nixiz-agent
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/agent/contract.json $(1)/usr/share/nixiz/contract.json
	$(INSTALL_BIN) ./files/usr/libexec/nixiz/package-lifecycle $(1)/usr/libexec/nixiz/package-lifecycle
	$(INSTALL_DIR) $(1)/lib/upgrade $(1)/lib/apk/commit_hooks.d
	$(INSTALL_BIN) ./files/lib/apk/commit_hooks.d/50-nixiz $(1)/lib/apk/commit_hooks.d/50-nixiz
	$(INSTALL_DATA) ./files/lib/upgrade/nixiz.sh $(1)/lib/upgrade/nixiz.sh
	(cd $(1); sha256sum usr/libexec/nixiz/agent etc/init.d/nixiz-agent usr/share/nixiz/contract.json > usr/share/nixiz/runtime.sha256)
endef

# An existing agent serializes this marker with transaction preparation. The
# first installation cannot race a transaction because no agent exists yet.
define Package/nixiz-agent/preinst
#!/bin/sh
[ -z "$${IPKG_INSTROOT}" ] || exit 0
if [ -x /usr/libexec/nixiz/package-lifecycle ]; then
	exec /usr/libexec/nixiz/package-lifecycle prepare-install
fi
[ ! -e /usr/libexec/nixiz/agent ] || exit 1
[ ! -L /etc/nixiz ] || exit 1
mkdir -p /etc/nixiz || exit 1
chmod 0700 /etc/nixiz || exit 1
[ ! -L /etc/nixiz/package-operation ] || exit 1
(umask 077; printf 'install\n' > /etc/nixiz/package-operation) || exit 1
exit 0
endef

define Package/nixiz-agent/postinst
#!/bin/sh
[ -z "$${IPKG_INSTROOT}" ] || exit 0
exec /usr/libexec/nixiz/package-lifecycle finish-install
endef

define Package/nixiz-agent/prerm
#!/bin/sh
[ -z "$${IPKG_INSTROOT}" ] || exit 0
exec /usr/libexec/nixiz/package-lifecycle prepare-remove
endef

$(eval $(call BuildPackage,nixiz-agent))
