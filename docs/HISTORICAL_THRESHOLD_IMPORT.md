# Historical CSV import for Threshold Lab

Use this when you do not have the Nesso hardware with you.

## Accepted CSV columns

Required sensor columns:

- Timestamp
- Accelerometer_X_g
- Accelerometer_Y_g
- Accelerometer_Z_g
- Gyroscope_X_deg_s
- Gyroscope_Y_deg_s
- Gyroscope_Z_deg_s

Label column:

- `label`, or
- `Fall_Label`

Labels accepted include `FALL` and `NO_FALL`.

`Event_ID` is optional but recommended. When present, each FALL Event_ID becomes one event-level training sample.

## Website workflow

1. Open Threshold Lab.
2. Enter the admin password.
3. Choose a marked CSV.
4. Click **Import labelled CSV**.
5. Repeat for another historical CSV if needed.
6. Check the FALL / NO_FALL counts.
7. Click **Train Decision Tree Recommendation**.

The backend stores extracted event-level features in `threshold_imported_samples`.
It does not duplicate the raw sensor CSV into Supabase.

Imported samples are combined with reviewed live incidents and background normal feature windows.

The Decision Tree remains recommendation-only and does not automatically change the live safety thresholds.

## Important evaluation note

Accuracy reflects the labels used for training/testing. Historical pseudo-labels are useful for prototyping, but independently verified controlled FALL / NO_FALL trials are stronger evidence for the final report.
