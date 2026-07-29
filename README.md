We have created a free tool to convert PrestaShop data into WooCommerce-compatible format.
You can use this tool to convert your Products, Customers, and Orders data into files that are ready to import into WooCommerce / WordPress.
Once converted, you can simply upload the new data files to WooCommerce using the WebToffee Import Export plugin.

Please see the detailed instructions at:

Please see the code and complete guide below.

**Step 1 — Install Python (one-time setup)**

Python is the free program that runs the script. If you already have Python installed, skip to Step 2.
 
- Go to python.org/downloads in your web browser.
- Click the yellow "Download Python" button.
- Open the downloaded file and run the installer.
 
Important:  On the first install screen, tick the box that says "Add Python to PATH" before clicking Install.
 
- Click Install Now and wait for it to finish.
 
To check it worked, open your terminal (Command Prompt on Windows, Terminal on Mac) and type:

 python --version
 

**Step 2 — Install the Required Add-ons**

The script needs two free add-on packages to read Excel/CSV files. Open your terminal and type this single line:

 pip install pandas openpyxl 
 
Press Enter and wait a few seconds for it to finish. You only need to do this once.


**Step 3 — Save Your Files in One Folder**

Create a new folder on your Desktop (for example, "PS-to-WooCommerce"). Inside it, create another folder called "input" — this is where all your PrestaShop export files will go.

Your folder structure should look like this:

PS-to-WooCommerce/
  prestashop_to_woocommerce.py
  input/
    prestashop_products.csv
    prestashop_customers.csv
    prestashop_address.csv
    prestashop_orders.csv

Place the script file directly inside your project folder, and place your PrestaShop CSV exports inside the input folder:

input/prestashop_products.csv (your PrestaShop product export — if migrating products)

input/prestashop_customers.csv (your PrestaShop customer export — if migrating customers)

input/prestashop_address.csv (your PrestaShop address export — matched to customers by id_customer)

input/prestashop_orders.csv (your PrestaShop order export — if migrating orders)

You do not need all four files. Only include the ones you want to convert.


**Step 4 — Run the Script**

1. Open your terminal.
2. Navigate to the folder you created. For example: cd Desktop/PS-to-WooCommerce 
3. Run the script by typing:
 
  python prestashop_to_woocommerce.py 

The script will automatically detect the file types and convert them. It also automatically merges your Addresses file into your Customers file by matching id_customer, so billing/shipping details come through without any extra steps. You can also run it with options:

python prestashop_to_woocommerce.py - If you want convert all (products, customers and orders) ( Recommended — merges addresses with customers automatically )

python prestashop_to_woocommerce.py --file ./input/prestashop_products.csv - Process a single file only - (If you want convert only products) (Important - Address merge is skipped in single-file mode) 

python prestashop_to_woocommerce.py --file ./input/prestashop_orders.csv - Process a single file only - (If you want convert only products) (Important - Address merge is skipped in single-file mode)


**Step 5 — Find Your Converted Files**

Once the script finishes, it creates a new folder called "output" inside your project folder. Open it to find:

woocommerce_products.csv (Your products, ready for WooCommerce)

woocommerce_customers.csv (Your customers, ready for WooCommerce)

woocommerce_orders.csv (Your orders, ready for WooCommerce)


**Step 6 — Import Into WooCommerce**

WooCommerce does not provide a built-in feature to import customers and orders. To import these, we use the WebToffee Import Export plugin, which offers a free version for importing customers and orders.
However, WooCommerce includes a built-in product import feature, so no additional plugin is required for importing products.

Products

4. In WordPress Admin, go to Products → All Products.
5. Click the Import button at the top.
6. Choose the file woocommerce_ products.csv and click Continue.
7. Map columns if prompted, then click Run the Importer.
 
Customers

WordPress does not have a built-in customer/user CSV importer. Using the webtoffee format (default), import with the WebToffee plugin’s Customer importer:

8. Go to WebToffee Import Export → Import.
9. Select User/Customer as the post type and choose woocommerce_customers.csv.
10. Map the columns (the “roles” column is already set to “customer” for every row, not “subscriber”).
11. Click Import. 

Orders

WooCommerce does not allow orders to be imported directly. Using the webtoffee format (default), import with the WebToffee plugin’s Order importer:

12. Import your products first (Step above) — order line items are linked by SKU, so matching products must already exist in the store.
13. Go to WebToffee Import Export → Import → select Order as the post type.
14. Choose woocommerce_orders.csv and proceed through column mapping.
15. On Step 4: Advanced Options, set “Link products using SKU instead of Product ID” to Yes – this is essential, since the file doesn’t contain your store’s internal product IDs.
16. Click Import.


**Troubleshooting — Common Questions**

**Problem - Solution**

"python" is not recognized  Reinstall Python and make sure to tick "Add Python to PATH"

“No module named pandas” Run: pip install pandas openpyxl

File detected as UNKNOWN Run with --inspect flag to see column names. Share the output and we will fix the mapping.

File not found Make sure the CSV file is in the same folder as the script, and you typed the exact file name (e.g. prestashop_products.csv)

“Error getting remote image” / images missing  The script checks every image link automatically and skips dead ones – see missing_images_report.csv and upload those images manually 

Customer and Address files don't merge Make sure both files are in the same input folder and run the whole folder together (not --file on each one). The tool matches rows by id_customer — export the full Addresses list without filters so every customer has a match.

Variable products not importing WooCommerce's built-in importer does not support variation rows. The script merges combinations into one parent row with all attributes listed.

Quick Reference — Every Time You Run It
 
- Open terminal in your project folder.
- Type:  python prestashop_to_woocommerce.py
- Find your results in the output folder.
- Import each CSV using the correct WebToffee plugin in WordPress Admin.
 
That's it — no coding required. If you run into any issue not listed above, run the script with the --inspect flag and check that your CSV files were exported correctly from PrestaShop.

At FirstWire, we can do the complete migration and make sure that your new Woocommerce Store is set up properly and optimized for Design, User Experience, Performance, SEO and CRO.

Please Contact Us for a custom proposal at https://firstwireapp.com/get-a-quotation/

You can also check our other WooCommerce Services at https://firstwireapp.com/e-commerce/shopify/
