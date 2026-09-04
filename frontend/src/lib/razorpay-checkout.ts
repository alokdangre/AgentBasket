export type RazorpaySuccess = {
  razorpay_order_id: string;
  razorpay_payment_id: string;
  razorpay_signature: string;
};

type RazorpayFailure = {
  error?: { description?: string };
};

type RazorpayInstance = {
  open: () => void;
  on?: (event: "payment.failed", handler: (failure: RazorpayFailure) => void) => void;
};

type RazorpayConstructor = new (options: {
  key: string;
  amount: number;
  currency: string;
  name: string;
  description: string;
  order_id: string;
  customer_id?: string;
  recurring?: boolean | 1;
  method?: { upi: boolean };
  prefill: { name: string; email: string; contact?: string };
  theme: { color: string };
  retry: { enabled: boolean };
  modal: { ondismiss: () => void };
  handler: (response: RazorpaySuccess) => void;
}) => RazorpayInstance;

declare global {
  interface Window {
    Razorpay?: RazorpayConstructor;
  }
}

let razorpayScriptPromise: Promise<void> | null = null;

export function loadRazorpayCheckout(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  if (razorpayScriptPromise) return razorpayScriptPromise;
  razorpayScriptPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://checkout.razorpay.com/v1/checkout.js";
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => {
      razorpayScriptPromise = null;
      reject(new Error("Razorpay Checkout could not load"));
    };
    document.head.appendChild(script);
  });
  return razorpayScriptPromise;
}
