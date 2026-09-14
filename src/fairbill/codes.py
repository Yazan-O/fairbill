"""Static plain-English code table for the gallery's CPT/HCPCS codes, plus the
panel/component relationships an unbundling check needs.

Descriptions are short patient-facing wording. PANELS maps a panel code to the
component codes the panel already includes; a bill that lists the components
separately when the hospital's own file prices the panel is unbundled.
"""
from __future__ import annotations

CODES: dict[str, tuple[str, str]] = {
    # (plain-English meaning, family)
    "85025": ("Complete blood count with automated differential (CBC with diff)", "lab-hematology"),
    "85027": ("Complete blood count, automated, without differential", "lab-hematology"),
    "85004": ("Automated differential white blood cell count", "lab-hematology"),
    "85007": ("Manual microscopic differential of white blood cells", "lab-hematology"),
    "85018": ("Hemoglobin level", "lab-hematology"),
    "85014": ("Hematocrit (red blood cell concentration)", "lab-hematology"),
    "85041": ("Red blood cell count, automated", "lab-hematology"),
    "85048": ("White blood cell count, automated", "lab-hematology"),
    "80053": ("Comprehensive metabolic panel, 14 blood chemistry tests", "lab-chemistry"),
    "80048": ("Basic metabolic panel, 8 blood chemistry tests", "lab-chemistry"),
    "80061": ("Lipid panel (cholesterol group)", "lab-chemistry"),
    "84443": ("Thyroid stimulating hormone (TSH)", "lab-chemistry"),
    "83036": ("Hemoglobin A1c (average blood sugar)", "lab-chemistry"),
    "82947": ("Blood glucose test", "lab-chemistry"),
    "82565": ("Creatinine, blood", "lab-chemistry"),
    "84132": ("Potassium, blood", "lab-chemistry"),
    "84295": ("Sodium, blood", "lab-chemistry"),
    "36415": ("Blood draw by needle stick (venipuncture)", "collection"),
    "96372": ("Therapeutic injection into muscle or under the skin", "injection"),
    "96360": ("Hydration IV infusion, first hour", "infusion"),
    "96361": ("Hydration IV infusion, each extra hour", "infusion"),
    "96374": ("IV push of a drug, first drug", "infusion"),
    "96375": ("IV push of an extra drug", "infusion"),
    "71046": ("Chest X-ray, 2 views", "imaging"),
    "71045": ("Chest X-ray, 1 view", "imaging"),
    "70450": ("CT scan of the head without contrast", "imaging"),
    "74177": ("CT scan of abdomen and pelvis with contrast", "imaging"),
    "74176": ("CT scan of abdomen and pelvis without contrast", "imaging"),
    "73630": ("X-ray of the foot, 3 or more views", "imaging"),
    "72110": ("X-ray of the lower spine, 4 or more views", "imaging"),
    "99281": ("Emergency department visit, level 1, straightforward problem", "ed-visit"),
    "99282": ("Emergency department visit, level 2, low complexity", "ed-visit"),
    "99283": ("Emergency department visit, level 3, moderate complexity", "ed-visit"),
    "99284": ("Emergency department visit, level 4, high complexity", "ed-visit"),
    "99285": ("Emergency department visit, level 5, high complexity with a threat to life or function", "ed-visit"),
    "G0463": ("Hospital outpatient clinic visit", "clinic-visit"),
    "99213": ("Office visit, established patient, low complexity", "office-visit"),
    "99214": ("Office visit, established patient, moderate complexity", "office-visit"),
    "99215": ("Office visit, established patient, high complexity", "office-visit"),
    "87086": ("Urine culture with colony count", "lab-micro"),
    "87088": ("Urine culture, identification of bacteria", "lab-micro"),
    "81001": ("Urinalysis, automated, with microscope exam", "lab-urine"),
    "81003": ("Urinalysis, automated, without microscope exam", "lab-urine"),
    "87880": ("Rapid strep test", "lab-micro"),
    "87804": ("Rapid influenza test", "lab-micro"),
    "93005": ("Electrocardiogram (ECG), tracing only", "cardiac"),
    "93010": ("Electrocardiogram (ECG), interpretation and report only", "cardiac"),
    "93000": ("Electrocardiogram (ECG), tracing with interpretation and report", "cardiac"),
    "93306": ("Echocardiogram of the heart with Doppler", "cardiac"),
    "45378": ("Diagnostic colonoscopy", "endoscopy"),
    "45380": ("Colonoscopy with biopsy", "endoscopy"),
    "45385": ("Colonoscopy with removal of a polyp by snare", "endoscopy"),
    "29881": ("Knee arthroscopy with meniscus repair or trim", "ortho-surgery"),
    "43239": ("Upper endoscopy with biopsy", "endoscopy"),
    "J1885": ("Ketorolac injection, 15 mg (anti-inflammatory pain drug)", "drug"),
    "J2270": ("Morphine injection, up to 10 mg", "drug"),
    "J0696": ("Ceftriaxone antibiotic injection, 250 mg", "drug"),
    "J7030": ("IV bag of normal saline, 1000 mL", "drug"),
    "J7040": ("IV bag of sterile saline, 500 mL", "drug"),
    "J3490": ("Injectable drug, not otherwise classified", "drug"),
    "J2550": ("Promethazine injection, up to 50 mg (nausea)", "drug"),
    "J1100": ("Dexamethasone injection, 1 mg (steroid)", "drug"),
    "J0171": ("Adrenalin (epinephrine) injection, 0.1 mg", "drug"),
    "97110": ("Physical therapy, therapeutic exercise, 15 minutes", "therapy"),
    "97140": ("Physical therapy, manual therapy, 15 minutes", "therapy"),
    "99152": ("Moderate sedation by the same doctor, first 15 minutes", "sedation"),
    "99153": ("Moderate sedation, each extra 15 minutes", "sedation"),
    "G0378": ("Hospital observation care, per hour", "observation"),
    "G0379": ("Direct admission to hospital observation care", "observation"),
    "00812": ("Anesthesia for a screening colonoscopy", "anesthesia"),
}

# Panel code -> component codes the panel already includes.
PANELS: dict[str, list[str]] = {
    "85025": ["85027", "85004", "85018", "85014", "85041", "85048", "85007"],
    "85027": ["85018", "85014", "85041", "85048"],
    "80053": ["80048", "82947", "82565", "84132", "84295"],
    "80048": ["82947", "82565", "84132", "84295"],
    "93000": ["93005", "93010"],
}

# Visit-level ladders: ordered low to high. Used for upcoding checks.
VISIT_LEVELS: dict[str, list[str]] = {
    "ed-visit": ["99281", "99282", "99283", "99284", "99285"],
    "office-visit": ["99213", "99214", "99215"],
}


def panels_containing(code: str) -> list[str]:
    """Panel codes whose components include `code`."""
    code = code.strip().upper()
    return [p for p, comps in PANELS.items() if code in comps]


def describe(code: str) -> dict:
    """Plain-English meaning plus family, panel and level relationships for one code."""
    code = (code or "").strip().upper()
    if code not in CODES:
        return {"found": False, "code": code, "meaning": None, "family": None}
    meaning, family = CODES[code]
    out = {
        "found": True,
        "code": code,
        "meaning": meaning,
        "family": family,
        "includes": PANELS.get(code, []),
        "component_of": panels_containing(code),
    }
    if family in VISIT_LEVELS:
        ladder = VISIT_LEVELS[family]
        out["level_ladder"] = ladder
        out["level"] = ladder.index(code) + 1
        out["level_note"] = (
            "Visit level must match the medical decision making documented in the note; "
            "a higher level costs more."
        )
    return out
