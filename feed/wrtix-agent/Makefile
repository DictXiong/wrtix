# SPDX-License-Identifier: GPL-2.0-only
WRTIX_SOURCE_DIR:=$(abspath $(dir $(realpath $(lastword $(MAKEFILE_LIST))))/../..)
include $(TOPDIR)/rules.mk

PKG_NAME:=wrtix-agent
PKG_VERSION:=0.2.0
PKG_RELEASE:=1
PKG_LICENSE:=GPL-2.0-only
PKGARCH:=all

include $(INCLUDE_DIR)/package.mk

define Package/wrtix-agent
  SECTION:=admin
  CATEGORY:=Administration
  TITLE:=Transactional declarative OpenWrt configuration agent
  DEPENDS:=+jq +uci +ubus +flock +coreutils-timeout
endef

define Package/wrtix-agent/description
  Configuration transactions, identity enrollment, independent rollback and
  recovery for wrtix. Installation preserves existing network and credentials.
endef

define Package/wrtix-agent/conffiles
/etc/wrtix/identity.json
/etc/wrtix/managed.json
/etc/wrtix/root-password-hash
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)/agent
	$(CP) $(WRTIX_SOURCE_DIR)/agent/. $(PKG_BUILD_DIR)/agent/
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/wrtix-agent/install
	$(INSTALL_DIR) $(1)/usr/libexec/wrtix $(1)/usr/share/wrtix $(1)/etc/init.d $(1)/etc/wrtix
	chmod 0700 $(1)/etc/wrtix
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/agent/agent.sh $(1)/usr/libexec/wrtix/agent
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/agent/guard.init $(1)/etc/init.d/wrtix-agent
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/agent/contract.json $(1)/usr/share/wrtix/contract.json
	$(INSTALL_BIN) ./files/usr/libexec/wrtix/package-lifecycle $(1)/usr/libexec/wrtix/package-lifecycle
	$(INSTALL_DIR) $(1)/lib/upgrade $(1)/lib/apk/commit_hooks.d
	$(INSTALL_BIN) ./files/lib/apk/commit_hooks.d/50-wrtix $(1)/lib/apk/commit_hooks.d/50-wrtix
	$(INSTALL_DATA) ./files/lib/upgrade/wrtix.sh $(1)/lib/upgrade/wrtix.sh
	(cd $(1); sha256sum usr/libexec/wrtix/agent etc/init.d/wrtix-agent usr/share/wrtix/contract.json > usr/share/wrtix/runtime.sha256)
endef

# An existing agent serializes this marker with transaction preparation. The
# first installation cannot race a transaction because no agent exists yet.
define Package/wrtix-agent/preinst
#!/bin/sh
[ -z "$${IPKG_INSTROOT}" ] || exit 0
if [ -x /usr/libexec/wrtix/package-lifecycle ]; then
	exec /usr/libexec/wrtix/package-lifecycle prepare-install
fi
[ ! -e /usr/libexec/wrtix/agent ] || exit 1
[ ! -L /etc/wrtix ] || exit 1
mkdir -p /etc/wrtix || exit 1
chmod 0700 /etc/wrtix || exit 1
[ ! -L /etc/wrtix/package-operation ] || exit 1
(umask 077; printf 'prepare-install\n' > /etc/wrtix/package-operation) || exit 1
exit 0
endef

define Package/wrtix-agent/postinst
#!/bin/sh
[ -z "$${IPKG_INSTROOT}" ] || exit 0
exec /usr/libexec/wrtix/package-lifecycle finish-install
endef

define Package/wrtix-agent/prerm
#!/bin/sh
[ -z "$${IPKG_INSTROOT}" ] || exit 0
exec /usr/libexec/wrtix/package-lifecycle prepare-remove
endef

$(eval $(call BuildPackage,wrtix-agent))
