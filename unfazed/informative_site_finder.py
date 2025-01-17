#! /usr/bin/env python
from __future__ import print_function

import sys
from concurrent.futures import ThreadPoolExecutor, wait

from cyvcf2 import VCF
from .utils import *

def get_position(vcf, denovo, extra, whole_region):
    locs = []
    loc_template = "{prefix}{chrom}:{start}-{end}"
    prefix = get_prefix(vcf)
    if whole_region:
        locs.append(
            loc_template.format(
                prefix=prefix,
                chrom=denovo["chrom"].strip("chr"),
                start=(int(denovo["start"]) - extra),
                end=(int(denovo["end"]) + extra),
            )
        )
    else:
        locs.append(
            loc_template.format(
                prefix=prefix,
                chrom=denovo["chrom"].strip("chr"),
                start=(int(denovo["start"]) - extra),
                end=(int(denovo["start"]) + extra),
            )
        )
        if (int(denovo["end"]) - int(denovo["start"])) > extra:
            locs.append(
                loc_template.format(
                    prefix=prefix,
                    chrom=denovo["chrom"].strip("chr"),
                    start=(int(denovo["end"]) - extra),
                    end=(int(denovo["end"]) + extra),
                )
            )
    for loc in locs:
        for variant in vcf(loc):
            yield variant


def is_high_quality_site(i, ref_depths, alt_depths, genotypes, gt_quals):
    """
    check if the potential informative site is high quality.
    this is pretty hacky, but filters out the really cruddy variants.
    i: index of the parent in the VCF FORMAT field
    ref_depths: numpy array of reference depths
    alt_depths: numpy array of alternate depths
    genotypes: numpy array of genotypes
    gt_quals: numpy array of genotype qualities
    """
    if genotypes[i] == HOM_REF:
        min_ab, max_ab = MIN_AB_HOMREF, MAX_AB_HOMREF
    elif genotypes[i] == HOM_ALT:
        min_ab, max_ab = MIN_AB_HOMALT, MAX_AB_HOMALT
    elif genotypes[i] == HET:
        min_ab, max_ab = MIN_AB_HET, MAX_AB_HET
    else: #gt is unknown
        return False
    if gt_quals[i] < MIN_GT_QUAL:
        return False
    if (ref_depths[i] + alt_depths[i]) < MIN_DEPTH:
        return False

    allele_bal = float(alt_depths[i] / float(ref_depths[i] + alt_depths[i]))

    if min_ab <= allele_bal <= max_ab:
        return True
    return False


def get_kid_allele(
    denovo, genotypes, ref_depths, alt_depths, kid_idx, dad_idx, mom_idx
):
    kid_allele = None
    if (denovo["vartype"] == "DEL") and (ref_depths[kid_idx] + alt_depths[kid_idx]) > 4:
        # large deletions can be genotyped by hemizygous inheritance of informative alleles
        if genotypes[kid_idx] == HOM_ALT:
            kid_allele = "ref_parent"
        elif genotypes[kid_idx] == HOM_REF:
            kid_allele = "alt_parent"
        else:
            # het kid, variant unusable
            return
    elif (
        (denovo["vartype"] == "DUP")
        and (ref_depths[kid_idx] > 2)
        and (alt_depths[kid_idx] > 2)
        and (ref_depths[kid_idx] + alt_depths[kid_idx]) > MIN_DEPTH
    ):
        # large duplications can be genotyped by unbalanced het inheritance
        # of informative alleles if there's enough depth
        if genotypes[kid_idx] == HET:
            kid_alt_allele_bal = alt_depths[kid_idx] / float(
                ref_depths[kid_idx] + alt_depths[kid_idx]
            )
            dad_alt_allele_bal = alt_depths[dad_idx] / float(
                ref_depths[dad_idx] + alt_depths[dad_idx]
            )
            mom_alt_allele_bal = alt_depths[mom_idx] / float(
                ref_depths[mom_idx] + alt_depths[mom_idx]
            )
            # DUPs can't be phased this way if the parental shared allele is duplicated, though
            # in first case, the dominate allele is alt and alt is duplicated, which is unphaseable
            # in second case, the dominate allele is ref and ref is duplicated, which is unphaseable
            if (
                ((dad_alt_allele_bal + mom_alt_allele_bal) < 1)
                and (kid_alt_allele_bal > 0.5)
                or ((dad_alt_allele_bal + mom_alt_allele_bal) > 1)
                and (kid_alt_allele_bal < 0.5)
            ):
                return

            # allele balance must be at least 2:1 for the dominant allele
            if kid_alt_allele_bal >= 0.67:
                # this variant came from the alt parent
                kid_allele = "alt_parent"
            elif kid_alt_allele_bal <= 0.33:
                # this variant came from the ref parent
                kid_allele = "ref_parent"
            else:
                # balanced allele, variant unusable
                return
        else:
            # homozygous kid, variant unusable
            return
    else:
        # not a CNV, or not enough depth
        return
    return kid_allele


def autophaseable(denovo, pedigrees, build):
    """
    variants in males on the X or Y chromosome and not in the
    pseudoautosomal regions can be automatically phased to the
    dad (if Y) or mom (if x)
    """
    chrom = denovo["chrom"].lower().strip("chr")
    if chrom not in ["y", "x"]:
        return False
    if int(pedigrees[denovo["kid"]]["sex"]) != SEX_KEY["male"]:
        return False
    if build not in ["37", "38"]:
        return False

    if build == "37":
        par1 = grch37_par1
        par2 = grch37_par2

    if build == "38":
        par1 = grch38_par1
        par2 = grch38_par2
    # variant is pseudoautosomal
    if (
        par1[chrom][0] <= denovo["start"] <= par1[chrom][1]
        or par2[chrom][0] <= denovo["start"] <= par2[chrom][1]
    ):
        return False
    return True


def find(
    dnms,
    pedigrees,
    vcf_name,
    search_dist,
    threads,
    build,
    multithread_proc_min,
    quiet_mode,
    ab_homref,
    ab_homalt,
    ab_het,
    min_gt_qual,
    min_depth,
    whole_region=True,
):
    """
    Given list of denovo variant positions
    a vcf_name, and the distance upstream or downstream to search, find informative sites
    """
    global QUIET_MODE
    QUIET_MODE = quiet_mode
    global MIN_AB_HET
    MIN_AB_HET = ab_het[0]
    global MIN_AB_HOMREF
    MIN_AB_HOMREF = ab_homref[0]
    global MIN_AB_HOMALT
    MIN_AB_HOMALT = ab_homalt[0]
    global MAX_AB_HET
    MAX_AB_HET = ab_het[1]
    global MAX_AB_HOMREF
    MAX_AB_HOMREF = ab_homref[1]
    global MAX_AB_HOMALT
    MAX_AB_HOMALT = ab_homalt[1]
    global MIN_GT_QUAL
    MIN_GT_QUAL = min_gt_qual
    global MIN_DEPTH
    MIN_DEPTH = min_depth

    if len(dnms) >= multithread_proc_min:
        return find_many(
            dnms, pedigrees, vcf_name, search_dist, threads, build, whole_region
        )
    elif len(dnms) <= 0:
        return

    vcf = VCF(vcf_name)
    sample_dict = dict(zip(vcf.samples, range(len(vcf.samples))))
    for i, denovo in enumerate(dnms):
        if autophaseable(denovo, pedigrees, build):
            continue
        kid_id = denovo["kid"]
        dad_id = pedigrees[denovo["kid"]]["dad"]
        mom_id = pedigrees[denovo["kid"]]["mom"]

        missing = False
        for sample_id in [kid_id, dad_id, mom_id]:
            if sample_id not in sample_dict:
                if not QUIET_MODE:
                    print("{} missing from SNV vcf/bcf", file=sys.stderr)
                missing = True
        if missing:
            continue
        kid_idx = sample_dict[kid_id]
        dad_idx = sample_dict[dad_id]
        mom_idx = sample_dict[mom_id]

        candidate_sites = []
        het_sites = []
        # loop over all variants in the VCF within search_dist bases from the DNM
        for variant in get_position(vcf, denovo, search_dist, whole_region):
            # ignore more complex variants for now
            if (
                len(variant.ALT) != 1
                or (len(variant.REF) > 1)
                or ("*" in variant.ALT or len(variant.ALT[0]) > 1)
            ):
                continue

            # male chrX variants have to come from mom
            if variant.CHROM == "X" and (
                pedigrees[denovo["kid"]]["sex"] == SEX_KEY["male"]
            ):
                continue

            # if this is a small event (SNV or INDEL), ignore candidate sites in the variant
            if ((denovo["end"] - denovo["start"]) < 20) and (
                variant.start in range(denovo["start"], denovo["end"])
            ):
                continue
            genotypes = variant.gt_types
            ref_depths = variant.gt_ref_depths
            alt_depths = variant.gt_alt_depths
            gt_quals = variant.gt_quals

            candidate = {
                "pos": variant.start,
                "ref_allele": variant.REF,
                "alt_allele": variant.ALT[0],
            }

            if (
                (genotypes[kid_idx] == HET)
                and is_high_quality_site(
                    dad_idx, ref_depths, alt_depths, genotypes, gt_quals
                )
                and is_high_quality_site(
                    mom_idx, ref_depths, alt_depths, genotypes, gt_quals
                )
            ):
                # variant usable for extended read-backed phasing
                het_sites.append(
                    {
                        "pos": variant.start,
                        "ref_allele": variant.REF,
                        "alt_allele": variant.ALT[0],
                    }
                )

            if whole_region and ("vartype" in denovo):
                candidate["kid_allele"] = get_kid_allele(
                    denovo, genotypes, ref_depths, alt_depths, kid_idx, dad_idx, mom_idx
                )
                if not candidate["kid_allele"]:
                    continue
            elif genotypes[kid_idx] != HET or not is_high_quality_site(
                kid_idx, ref_depths, alt_depths, genotypes, gt_quals
            ):
                continue

            if not (
                is_high_quality_site(
                    dad_idx, ref_depths, alt_depths, genotypes, gt_quals
                )
                and is_high_quality_site(
                    mom_idx, ref_depths, alt_depths, genotypes, gt_quals
                )
            ):
                continue

            if genotypes[dad_idx] in (HET, HOM_ALT) and genotypes[mom_idx] == HOM_REF:
                candidate["alt_parent"] = pedigrees[denovo["kid"]]["dad"]
                candidate["ref_parent"] = pedigrees[denovo["kid"]]["mom"]
            elif genotypes[mom_idx] in (HET, HOM_ALT) and genotypes[dad_idx] == HOM_REF:
                candidate["alt_parent"] = pedigrees[denovo["kid"]]["mom"]
                candidate["ref_parent"] = pedigrees[denovo["kid"]]["dad"]
            elif genotypes[mom_idx] == HET and genotypes[dad_idx] == HOM_ALT:
                candidate["alt_parent"] = pedigrees[denovo["kid"]]["dad"]
                candidate["ref_parent"] = pedigrees[denovo["kid"]]["mom"]
            elif genotypes[dad_idx] == HET and genotypes[mom_idx] == HOM_ALT:
                candidate["alt_parent"] = pedigrees[denovo["kid"]]["mom"]
                candidate["ref_parent"] = pedigrees[denovo["kid"]]["dad"]
            else:
                continue

            # if kid is hemizygous we need to make sure the inherited allele is not shared
            # by both parents
            if genotypes[kid_idx] in [HOM_ALT, HOM_REF]:
                unique_allele = True
                # if one parent is het and the other is homozygous for either allele
                # make sure the kid doesn't have that allele
                parent_gts = [genotypes[dad_idx], genotypes[mom_idx]]
                if (HET in parent_gts) and (
                    HOM_ALT in parent_gts or HOM_REF in parent_gts
                ):
                    for parent_gt in parent_gts:
                        kid_gt = genotypes[kid_idx]
                        if (parent_gt in [HOM_ALT, HOM_REF]) and (kid_gt == parent_gt):
                            unique_allele = False
                if not unique_allele:
                    continue

            candidate_sites.append(candidate)

        denovo["candidate_sites"] = sorted(candidate_sites, key=lambda x: x["pos"])
        denovo["het_sites"] = sorted(het_sites, key=lambda x: x["pos"])
        dnms[i] = denovo
    return dnms


def create_lookups(dnms, pedigrees, build):
    """
    this will be a lookup to find samples for a range where
    variants are informative for a given denovo
    """
    samples_by_location = {}
    vars_by_sample = {}
    chrom_ranges = {}
    dnms_autophase = []
    dnms_nonautophase = []
    for denovo in dnms:
        if autophaseable(denovo, pedigrees, build):
            dnms_autophase.append(denovo)
            continue
        dnms_nonautophase.append(denovo)

        chrom = denovo["chrom"]
        start = int(denovo["start"])
        end = int(denovo["end"])
        sample = denovo["kid"]

        # find the range within the chromosome where we need data
        if chrom not in chrom_ranges:
            chrom_ranges[chrom] = [start, end]
        if start < chrom_ranges[chrom][0]:
            chrom_ranges[chrom][0] = start
        if end > chrom_ranges[chrom][1]:
            chrom_ranges[chrom][1] = end


        if sample not in vars_by_sample:
            vars_by_sample[sample] = {}
        if chrom not in vars_by_sample[sample]:
            vars_by_sample[sample][chrom] = {}
        if start not in vars_by_sample[sample][chrom]:
            vars_by_sample[sample][chrom][start] = []
        vars_by_sample[sample][chrom][start].append(denovo)

        if chrom not in samples_by_location:
            samples_by_location[chrom] = {}

        if start not in samples_by_location[chrom]:
            samples_by_location[chrom][start] = []
        samples_by_location[chrom][start].append(sample)

        if (end - start) > 2:
            if end not in samples_by_location[chrom] and ((end - start) > 2):
                samples_by_location[chrom][end] = []
            samples_by_location[chrom][end].append(sample)
    return samples_by_location, vars_by_sample, chrom_ranges, dnms_autophase, dnms_nonautophase


def get_close_vars(
    chrom, pos, samples_by_location, vars_by_sample, search_dist, whole_region
):
    close_var_keys = []
    if chrom not in samples_by_location:
        return close_var_keys
    if not whole_region:
        for dn_loc in samples_by_location[chrom]:
            if (dn_loc - search_dist) <= pos <= (dn_loc + search_dist):
                close_samples = samples_by_location[chrom][dn_loc]
                for close_sample in close_samples:
                    close_var_keys.append([close_sample, chrom, dn_loc])
    else:
        for dn_loc in samples_by_location[chrom]:
            close_samples = samples_by_location[chrom][dn_loc]
            for close_sample in close_samples:
                for denovo in vars_by_sample[close_sample][chrom][dn_loc]:
                    dn_start = int(denovo["start"])
                    dn_end = int(denovo["end"])
                    if (dn_start - search_dist) <= pos <= (dn_end + search_dist):
                        close_var_keys.append([close_sample, chrom, dn_start])
    return close_var_keys


def get_family_indexes(kid, pedigrees, sample_dict):
    dad_id = pedigrees[kid]["dad"]
    mom_id = pedigrees[kid]["mom"]
    missing = False
    for sample_id in [kid, dad_id, mom_id]:
        if sample_id not in sample_dict:
            if not QUIET_MODE:
                print("{} missing from SNV bcf", file=sys.stderr)
            missing = True
    if missing:
        return None, None, None

    kid_idx = sample_dict[kid]
    dad_idx = sample_dict[dad_id]
    mom_idx = sample_dict[mom_id]

    return kid_idx, dad_idx, mom_idx


def add_good_candidate_variant(
    variant, vars_by_sample, dn_key, pedigrees, whole_region, sample_dict, build
):
    kid, chrom, pos = dn_key
    kid_idx, dad_idx, mom_idx = get_family_indexes(kid, pedigrees, sample_dict)
    dad = pedigrees[kid]["dad"]
    mom = pedigrees[kid]["mom"]
    if None in [kid_idx, dad_idx, mom_idx]:
        return False
    if pos not in vars_by_sample[kid][chrom]:
        return False

    for i, denovo in enumerate(vars_by_sample[kid][chrom][pos]):
        if autophaseable(denovo, pedigrees, build):
            continue
        # if this is a small event (SNV or INDEL), ignore candidate sites in the variant
        if ((denovo["end"] - denovo["start"]) < 20) and (
            variant.start in range(denovo["start"], denovo["end"])
        ):
            continue
        genotypes = variant.gt_types
        ref_depths = variant.gt_ref_depths
        alt_depths = variant.gt_alt_depths
        gt_quals = variant.gt_quals

        candidate = {
            "pos": variant.start,
            "ref_allele": variant.REF,
            "alt_allele": variant.ALT[0],
        }
        if (
            (genotypes[kid_idx] == HET)
            and is_high_quality_site(
                dad_idx, ref_depths, alt_depths, genotypes, gt_quals
            )
            and is_high_quality_site(
                mom_idx, ref_depths, alt_depths, genotypes, gt_quals
            )
        ):
            if "het_sites" not in vars_by_sample[kid][chrom][pos][i]:
                vars_by_sample[kid][chrom][pos][i]["het_sites"] = []
            # variant usable for extended read-backed phasing
            vars_by_sample[kid][chrom][pos][i]["het_sites"].append(
                {
                    "pos": variant.start,
                    "ref_allele": variant.REF,
                    "alt_allele": variant.ALT[0],
                }
            )

        if whole_region and ("vartype" in denovo):
            candidate["kid_allele"] = get_kid_allele(
                denovo, genotypes, ref_depths, alt_depths, kid_idx, dad_idx, mom_idx
            )
            if not candidate["kid_allele"]:
                continue
        elif genotypes[kid_idx] != HET or not is_high_quality_site(
            kid_idx, ref_depths, alt_depths, genotypes, gt_quals
        ):
            continue

        if not (
            is_high_quality_site(dad_idx, ref_depths, alt_depths, genotypes, gt_quals)
            and is_high_quality_site(
                mom_idx, ref_depths, alt_depths, genotypes, gt_quals
            )
        ):
            continue

        if genotypes[dad_idx] in (HET, HOM_ALT) and genotypes[mom_idx] == HOM_REF:
            candidate["alt_parent"] = dad
            candidate["ref_parent"] = mom
        elif genotypes[mom_idx] in (HET, HOM_ALT) and genotypes[dad_idx] == HOM_REF:
            candidate["alt_parent"] = mom
            candidate["ref_parent"] = dad
        elif genotypes[mom_idx] == HET and genotypes[dad_idx] == HOM_ALT:
            candidate["alt_parent"] = dad
            candidate["ref_parent"] = mom
        elif genotypes[dad_idx] == HET and genotypes[mom_idx] == HOM_ALT:
            candidate["alt_parent"] = mom
            candidate["ref_parent"] = dad
        else:
            continue

        # if kid is hemizygous we need to make sure the inherited allele is not shared
        # by both parents
        if genotypes[kid_idx] in [HOM_ALT, HOM_REF]:
            unique_allele = True
            # if one parent is het and the other is homozygous for either allele
            # make sure the kid doesn't have that allele
            parent_gts = [genotypes[dad_idx], genotypes[mom_idx]]
            if (HET in parent_gts) and (HOM_ALT in parent_gts or HOM_REF in parent_gts):
                for parent_gt in parent_gts:
                    kid_gt = genotypes[kid_idx]
                    if (parent_gt in [HOM_ALT, HOM_REF]) and (kid_gt == parent_gt):
                        unique_allele = False
            if not unique_allele:
                continue
        if "candidate_sites" not in vars_by_sample[kid][chrom][pos][i]:
            vars_by_sample[kid][chrom][pos][i]["candidate_sites"] = []
        vars_by_sample[kid][chrom][pos][i]["candidate_sites"].append(candidate)
    return True


###################################################################################
def multithread_find_many(
    vcf_name,
    chrom,
    chrom_range,
    samples_by_location,
    vars_by_sample,
    search_dist,
    pedigrees,
    whole_region,
    build,
):
    vcf = VCF(vcf_name)
    prefix = get_prefix(vcf)
    sample_dict = dict(zip(vcf.samples, range(len(vcf.samples))))
    search_string = "{}:{}-{}".format(
        prefix + chrom.strip("chr"),
        chrom_range[0] - search_dist,
        chrom_range[1] + search_dist,
    )
    vcf_iter = vcf(search_string)

    for variant in vcf_iter:

        # ignore complex variants for now
        if (
            len(variant.ALT) != 1
            or (len(variant.REF) > 1)
            or ("*" in variant.ALT or len(variant.ALT[0]) > 1)
        ):
            continue

        close_var_keys = get_close_vars(
            variant.CHROM,
            variant.POS,
            samples_by_location,
            vars_by_sample,
            search_dist,
            whole_region,
        )

        if len(close_var_keys) == 0:
            continue
        for close_var_key in close_var_keys:
            add_good_candidate_variant(
                variant,
                vars_by_sample,
                close_var_key,
                pedigrees,
                whole_region,
                sample_dict,
                build,
            )


def find_many(
    dnms, pedigrees, vcf_name, search_dist, threads, build, whole_region=True
):
    """
    Given list of denovo variant positions
    a vcf_name, and the distance upstream or downstream to search, find informative sites
    """
    samples_by_location, vars_by_sample, chrom_ranges,dnms_autophase,dnms = create_lookups(
        dnms, pedigrees, build
    )
    chroms = set([dnm["chrom"] for dnm in dnms])

    if threads != 1:
        executor = ThreadPoolExecutor(threads)
        futures = []
    for chrom in chroms:
        if threads != 1:
            futures.append(
                executor.submit(
                    multithread_find_many,
                    vcf_name,
                    chrom,
                    chrom_ranges[chrom],
                    samples_by_location,
                    vars_by_sample,
                    search_dist,
                    pedigrees,
                    whole_region,
                    build,
                )
            )
        else:
            multithread_find_many(
                vcf_name,
                chrom,
                chrom_ranges[chrom],
                samples_by_location,
                vars_by_sample,
                search_dist,
                pedigrees,
                whole_region,
                build,
            )
    if threads != 1:
        wait(futures)

    dnms_annotated = []
    for sample in vars_by_sample:
        for chrom in vars_by_sample[sample]:
            for pos in vars_by_sample[sample][chrom]:
                for denovo in vars_by_sample[sample][chrom][pos]:
                    if "candidate_sites" in denovo:
                        denovo["candidate_sites"] = sorted(
                            denovo["candidate_sites"], key=lambda x: x["pos"]
                        )
                    if "het_sites" in denovo:
                        denovo["het_sites"] = sorted(
                            denovo["het_sites"], key=lambda x: x["pos"]
                        )
                    dnms_annotated.append(denovo)
    return (dnms_annotated+dnms_autophase)


if __name__ == "__main__":
    sys.exit("Import this as a module")
