import Link from "next/link";

import styles from "@/styles/storefront.module.css";

const columns = [
  {
    heading: "Shop",
    links: ["All products", "Gift cards"],
  },
  {
    heading: "Coffee",
    links: ["Whole bean", "Ground coffee"],
  },
  {
    heading: "Tea",
    links: ["Loose-leaf tea", "Teaware"],
  },
  {
    heading: "Café",
    links: ["Beverages", "Menu"],
  },
  {
    heading: "Help",
    links: ["FAQs", "Shipping & delivery"],
  },
];

export function SiteFooter() {
  return (
    <footer className={styles.footer}>
      <div className={styles.footerBrand}>
        <span>Ember &amp; Leaf</span>
        <small>Indiranagar, Bengaluru</small>
      </div>
      <div className={styles.footerColumns}>
        {columns.map((column) => (
          <div key={column.heading}>
            <strong>{column.heading}</strong>
            {column.links.map((link) => (
              <Link key={link} href="/shop">
                {link}
              </Link>
            ))}
          </div>
        ))}
      </div>
      <div className={styles.footerBottom}>
        <span>© 2026 Ember &amp; Leaf</span>
        <span>Privacy · Terms</span>
      </div>
    </footer>
  );
}
