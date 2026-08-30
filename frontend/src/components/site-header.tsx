"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  ChevronDownIcon,
  CloseIcon,
  MenuIcon,
  PinIcon,
  SearchIcon,
  UserIcon,
} from "@/components/icons";
import { CartIndicator } from "@/components/cart-indicator";
import styles from "@/styles/storefront.module.css";

const navigation = [
  { label: "Shop", href: "/shop" },
  { label: "Coffee", href: "/shop?type=packaged_coffee" },
  { label: "Tea", href: "/shop?type=packaged_tea" },
  { label: "Café", href: "/shop?type=prepared_beverage" },
];

type SiteHeaderProps = {
  postalCode: string;
};

export function SiteHeader({ postalCode }: SiteHeaderProps) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [locationOpen, setLocationOpen] = useState(false);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMobileOpen(false);
        setSearchOpen(false);
        setLocationOpen(false);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    document.body.style.overflow = mobileOpen ? "hidden" : "";
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
      document.body.style.overflow = "";
    };
  }, [mobileOpen]);

  return (
    <header className={styles.header}>
      <Link href="/" className={styles.brand} aria-label="Ember & Leaf home">
        Ember &amp; Leaf
      </Link>

      <nav className={styles.desktopNav} aria-label="Primary navigation">
        {navigation.map((item) => (
          <Link key={item.label} href={item.href} className={styles.navLink}>
            {item.label}
          </Link>
        ))}
      </nav>

      <div className={styles.headerActions}>
        <div className={styles.locationWrap}>
          <button
            type="button"
            className={styles.locationButton}
            aria-expanded={locationOpen}
            aria-controls="location-panel"
            onClick={() => setLocationOpen((open) => !open)}
          >
            <PinIcon />
            <span className={styles.locationDesktop}>Delivering to </span>
            <span>{postalCode}</span>
            <ChevronDownIcon className={styles.chevron} />
          </button>
          {locationOpen ? (
            <form id="location-panel" action="/" className={styles.locationPanel}>
              <label htmlFor="postal-code">Delivery postcode</label>
              <div>
                <input
                  id="postal-code"
                  name="postal_code"
                  inputMode="numeric"
                  pattern="[0-9]{6}"
                  minLength={6}
                  maxLength={6}
                  defaultValue={postalCode}
                  required
                />
                <button type="submit">Check</button>
              </div>
            </form>
          ) : null}
        </div>

        <button
          type="button"
          className={styles.iconButton}
          aria-label="Search products"
          aria-expanded={searchOpen}
          aria-controls="search-panel"
          onClick={() => setSearchOpen((open) => !open)}
        >
          <SearchIcon />
        </button>

        <Link href="/account" className={styles.accountButton} aria-label="Your account">
          <UserIcon />
        </Link>

        <CartIndicator />

        <a
          href="#ask-ember"
          className={styles.askButton}
          onClick={(event) => {
            event.preventDefault();
            window.dispatchEvent(new Event("ask-ember:open"));
          }}
        >
          Ask Ember
        </a>

        <button
          type="button"
          className={styles.mobileMenuButton}
          aria-label={mobileOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={mobileOpen}
          aria-controls="mobile-navigation"
          onClick={() => setMobileOpen((open) => !open)}
        >
          {mobileOpen ? <CloseIcon /> : <MenuIcon />}
        </button>
      </div>

      {searchOpen ? (
        <form id="search-panel" action="/" className={styles.searchPanel}>
          <SearchIcon />
          <label className={styles.srOnly} htmlFor="site-search">
            Search coffee, tea and café drinks
          </label>
          <input
            id="site-search"
            name="query"
            type="search"
            placeholder="Search coffee, tea and café drinks"
            autoFocus
          />
          <input type="hidden" name="postal_code" value={postalCode} />
          <button type="submit">Search</button>
        </form>
      ) : null}

      <div
        id="mobile-navigation"
        className={mobileOpen ? styles.mobileNavOpen : styles.mobileNav}
        aria-hidden={!mobileOpen}
      >
        <nav aria-label="Mobile navigation">
          {navigation.map((item) => (
            <Link
              key={item.label}
              href={item.href}
              onClick={() => setMobileOpen(false)}
            >
              {item.label}
              <span aria-hidden="true">↗</span>
            </Link>
          ))}
          <Link href="/account" onClick={() => setMobileOpen(false)}>
            Account
            <span aria-hidden="true">↗</span>
          </Link>
        </nav>
        <a
          href="#ask-ember"
          onClick={(event) => {
            event.preventDefault();
            setMobileOpen(false);
            window.dispatchEvent(new Event("ask-ember:open"));
          }}
        >
          Ask Ember
        </a>
      </div>
    </header>
  );
}
