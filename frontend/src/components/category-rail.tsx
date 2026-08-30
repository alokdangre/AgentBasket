import Link from "next/link";
import type { ReactNode } from "react";

import {
  ArrowRightIcon,
  BeansIcon,
  BrewerIcon,
  CupIcon,
  LeafIcon,
} from "@/components/icons";
import styles from "@/styles/storefront.module.css";

const categories: Array<{
  label: string;
  href: string;
  icon: ReactNode;
}> = [
  {
    label: "Prepared today",
    href: "/shop?type=prepared_beverage",
    icon: <CupIcon />,
  },
  {
    label: "Freshly roasted",
    href: "/shop?type=packaged_coffee",
    icon: <BeansIcon />,
  },
  {
    label: "Loose-leaf tea",
    href: "/shop?type=packaged_tea",
    icon: <LeafIcon />,
  },
  {
    label: "Brewing essentials",
    href: "/shop?type=accessory",
    icon: <BrewerIcon />,
  },
];

export function CategoryRail() {
  return (
    <nav className={styles.categoryRail} aria-label="Shop by category">
      {categories.map((category) => (
        <Link key={category.label} href={category.href}>
          <span className={styles.categoryIcon}>{category.icon}</span>
          <span>{category.label}</span>
          <ArrowRightIcon />
        </Link>
      ))}
    </nav>
  );
}
